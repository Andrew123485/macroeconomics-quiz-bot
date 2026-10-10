import os
import json
import asyncio
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv
import matplotlib.pyplot as plt
import io

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InputFile, FSInputFile, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from google import genai

load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(BASE_DIR, 'questions_db_blocks.json')

with open(db_path, 'r', encoding='utf-8') as f:
    questions = json.load(f)

# Pre-extract blocks in their natural order
blocks_order = []
for q in questions:
    if q['block_name'] not in blocks_order:
        blocks_order.append(q['block_name'])

print(f"Loaded {len(questions)} sorted questions across {len(blocks_order)} blocks")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

def ask_gemini(question_text, correct_answer, user_answer):
    prompt = f"""Ученик решает задания по экономике.
Вопрос: {question_text}
Его неверный ответ: {user_answer}
Правильный ответ: {correct_answer}

Объясни ученику коротко (1-3 предложения), почему его ответ неверный, и поясни правильный. Обязательно в конце выдели отдельным абзацем простое правило или короткую ассоциацию для быстрого запоминания."""
    try:
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return response.text
    except Exception as e:
        print(f"Gemini Error: {e}")
        return f"Правильный ответ: {correct_answer}. (Не удалось сгенерировать объяснение)"

class QuizState(StatesGroup):
    answering = State() 

SESSIONS_FILE = os.path.join(BASE_DIR, 'user_sessions_v3.json')

def load_sessions():
    if os.path.exists(SESSIONS_FILE):
        with open(SESSIONS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_sessions(sessions):
    with open(SESSIONS_FILE, 'w', encoding='utf-8') as f:
        json.dump(sessions, f, ensure_ascii=False, indent=2)

sessions = load_sessions()

INTRO_CACHE_FILE = os.path.join(BASE_DIR, 'block_intros_cache.json')
def load_intros():
    if os.path.exists(INTRO_CACHE_FILE):
        with open(INTRO_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_intros(intros):
    with open(INTRO_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(intros, f, ensure_ascii=False, indent=2)

intros_cache = load_intros()

def get_session(user_id):
    str_id = str(user_id)
    if str_id not in sessions:
        sessions[str_id] = {
            "answered": [],
            "last_level": None,
            "score": 0,
            "asked": 0,
            "errors": {}
        }
        
    s = sessions[str_id]
    # Ensure new fields exist for backward compatibility
    if "xp" not in s: s["xp"] = 0
    if "streak" not in s: s["streak"] = 0
    if "last_active_date" not in s: s["last_active_date"] = ""
    if "spaced_repetition" not in s: s["spaced_repetition"] = {}
    if "block_intros_seen" not in s: s["block_intros_seen"] = []
    
    return s

def get_next_question(session):
    answered = set(session.get("answered", []))
    last_level = session.get("last_level", None)
    
    current_block = None
    unanswered_in_block = []
    
    for b in blocks_order:
        b_qs = [q for q in questions if q['block_name'] == b and q['id'] not in answered]
        if b_qs:
            current_block = b
            unanswered_in_block = b_qs
            break
            
    if not current_block:
        return None
        
    levels = {}
    for q in unanswered_in_block:
        l = q['level_name']
        if l not in levels:
            levels[l] = []
        levels[l].append(q)
        
    sorted_levels = sorted(list(levels.keys()))
    
    next_level = sorted_levels[0]
    if last_level in sorted_levels:
        idx = sorted_levels.index(last_level)
        next_idx = (idx + 1) % len(sorted_levels)
        next_level = sorted_levels[next_idx]
    elif last_level:
        cands = [l for l in sorted_levels if l > last_level]
        if cands:
            next_level = cands[0]
        else:
            next_level = sorted_levels[0]

    return levels[next_level][0]

def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="Решать 60 вопросов")
    builder.button(text="Работа над ошибками")
    builder.button(text="🎓 Симуляция экзамена")
    builder.button(text="📊 Статистика")
    builder.adjust(2, 2)
    return builder.as_markup(resize_keyboard=True)

async def send_question(message: Message, user_id: int, state: FSMContext, count_for_today: int):
    session = get_session(user_id)
    data = await state.get_data()
    mode = data.get("mode", "normal")
    questions_left_today = data.get("questions_left_today", count_for_today)
    
    if mode == "normal":
        if questions_left_today <= 0:
            await bot.send_message(chat_id=user_id, text=f"На сегодня всё! Отличная работа. Вы можете посмотреть статистику или начать новую тренировку.", reply_markup=get_main_keyboard())
            await state.clear()
            save_sessions(sessions)
            return
            
        q = get_next_question(session)
        if not q:
            await bot.send_message(chat_id=user_id, text=f"Поздравляем! Вы решили АБСОЛЮТНО все вопросы во всех блоках базы данных!", reply_markup=get_main_keyboard())
            await state.clear()
            save_sessions(sessions)
            return
            
        # Check if we need to send a cheat sheet
        b_name = q['block_name']
        if b_name not in session.get('block_intros_seen', []):
            await bot.send_message(chat_id=user_id, text=f"📚 Вы начинаете новый блок: *{b_name}*.\n_Генерирую мини-конспект от ИИ..._", parse_mode="Markdown")
            
            cheat_sheet = intros_cache.get(b_name)
            if not cheat_sheet:
                prompt = f"Составь краткую шпаргалку (определения и главные формулы) на 1 экран смартфона по экономическому разделу: {b_name}. Пиши понятно, без лишней воды. Структурируй."
                try:
                    resp = await asyncio.get_event_loop().run_in_executor(
                        None, 
                        lambda: gemini_client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text
                    )
                    cheat_sheet = resp.replace('*', '').replace('_', '').replace('`', '')
                    intros_cache[b_name] = cheat_sheet
                    save_intros(intros_cache)
                except Exception as e:
                    cheat_sheet = f"(Не удалось сгенерировать конспект: {e})"
            
            await bot.send_message(chat_id=user_id, text=f"📖 *Ваша шпаргалка перед стартом:*\n\n{cheat_sheet}", parse_mode="Markdown")
            session['block_intros_seen'].append(b_name)
            save_sessions(sessions)
            await asyncio.sleep(2)
            
    elif mode == "exam":
        exam_queue = data.get("exam_queue", [])
        if not exam_queue:
            exam_score = data.get("exam_score", 0)
            exam_total = data.get("exam_total", 0)
            xp_gain = exam_score * 5
            session["xp"] += xp_gain
            save_sessions(sessions)
            await bot.send_message(chat_id=user_id, text=f"🎓 *Экзамен завершен!*\nВаш результат: {exam_score} из {exam_total}.\nПолучено опыта: {xp_gain} XP 🏆", parse_mode="Markdown", reply_markup=get_main_keyboard())
            await state.clear()
            return
            
        q_id = exam_queue[0]
        q = next((x for x in questions if x['id'] == q_id), None)
        if not q:
            exam_queue.pop(0)
            await state.update_data(exam_queue=exam_queue)
            await send_question(message, user_id, state, count_for_today)
            return
            
    else: # errors mode
        error_queue = data.get("error_queue", [])
        if questions_left_today <= 0 or not error_queue:
            await bot.send_message(chat_id=user_id, text=f"Работа над ошибками завершена!", reply_markup=get_main_keyboard())
            await state.clear()
            save_sessions(sessions)
            return
            
        q_id_str = error_queue[0]
        q = next((x for x in questions if str(x['id']) == q_id_str), None)
        if not q:
            error_queue.pop(0)
            await state.update_data(error_queue=error_queue)
            await send_question(message, user_id, state, count_for_today)
            return

    q_id = q['id']

    for img in q['images']:
        img_path = os.path.join(BASE_DIR, 'images', img)
        if os.path.exists(img_path):
            photo = FSInputFile(img_path)
            await bot.send_photo(chat_id=user_id, photo=photo)

    text = f"*{q['block_name']}*\n_{q['level_name']}_\nВопрос {q_id}:\n{q['question']}\n\n"
    
    markup = None
    
    if q['type'] == 'single':
        builder = InlineKeyboardBuilder()
        for i, ans in enumerate(q['answers']):
            text += f"{i+1}) {ans['text']}\n"
            builder.button(text=str(i+1), callback_data=f"ans_{q_id}_{i}")
        builder.adjust(1)
        markup = builder.as_markup()
        await state.set_state(QuizState.answering)
        await state.update_data(current_q=q, questions_left_today=questions_left_today)
        
    elif q['type'] == 'multiple':
        for i, ans in enumerate(q['answers']):
            text += f"{i+1}) {ans['text']}\n"
        text += "\n*(Ответ содержит несколько вариантов)*. Выберите нужные из списка ниже, а затем нажмите «Ответить»."
        
        draft = []
        markup = build_multiple_keyboard(q_id, q['answers'], draft)
        await state.set_state(QuizState.answering)
        await state.update_data(current_q=q, questions_left_today=questions_left_today, mult_draft=draft)
            
    elif q['type'] == 'matching':
        # Advanced Matching Feature
        # Shuffle R options
        all_R = []
        L_list = []
        for a in q['answers']:
            if a.get('match'):
                all_R.append(a['match'])
                L_list.append(a['text'])
        for a in q['answers']:
            if not a.get('match') and a.get('original_r'):
                all_R.append(a.get('original_r'))
                
        random.shuffle(all_R)
        r_map = {str(i+1): r for i, r in enumerate(all_R)}
        
        text += "*Сопоставьте понятия:*\n"
        for i, L in enumerate(L_list):
            text += f"*L{i+1}:* {L}\n"
            
        text += "\n*Варианты (R):*\n"
        for k, v in r_map.items():
            text += f"*R{k}:* {v}\n"
            
        text += "\nИспользуйте кнопки ниже для выбора пар."
        
        draft = {f"L{i+1}": None for i in range(len(L_list))}
        
        markup = build_matching_keyboard(q_id, draft, r_map)
        await state.set_state(QuizState.answering)
        await state.update_data(current_q=q, questions_left_today=questions_left_today, matching_draft=draft, r_map=r_map, L_list=L_list)
        
    else:
        text += "Впишите ваш ответ текстом в чат:"
        for ans in q['answers']:
            text += f"- {ans['text']}\n"
        await state.set_state(QuizState.answering)
        await state.update_data(current_q=q, questions_left_today=questions_left_today)

    await bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown", reply_markup=markup)

def build_matching_keyboard(q_id, draft, r_map):
    builder = InlineKeyboardBuilder()
    
    # 1 row per L
    for L_key in draft.keys():
        row_buttons = []
        # show current selected
        current_sel = draft[L_key] 
        for ri in r_map.keys():
            label = f"{L_key}-R{ri}"
            if current_sel == ri:
                label = f"✅ L{L_key[1:]}-R{ri}"
            cb_data = f"match_{q_id}_{L_key}_{ri}"
            builder.button(text=label, callback_data=cb_data)
        
        # Adjust row length so it doesn't wrap awkwardly
        # Actually, standard inline keyboard handles 8 buttons per row well 
    
    builder.adjust(len(r_map.keys())) # Force rows to be equal to number of R options (1 row per L)
    
    # Submit button
    builder.row(InlineKeyboardButton(text="Ответить ➡️", callback_data=f"matchsub_{q_id}"))
    return builder.as_markup()

def build_multiple_keyboard(q_id, answers, draft):
    builder = InlineKeyboardBuilder()
    for i in range(len(answers)):
        marker = "✅ " if i in draft else ""
        builder.button(text=f"{marker}{i+1}", callback_data=f"mul_{q_id}_{i}")
    builder.adjust(min(len(answers), 5)) 
    builder.row(InlineKeyboardButton(text="Ответить ➡️", callback_data=f"mulsub_{q_id}"))
    return builder.as_markup()

@dp.callback_query(F.data.startswith("mul_"))
async def handle_mul_click(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    q = data.get("current_q")
    if not q:
        await callback.answer("Сессия истекла.", show_alert=True)
        return
        
    parts = callback.data.split('_')
    q_id_str, idx_str = parts[1], parts[2]
    idx = int(idx_str)
    
    draft = data.get('mult_draft', [])
    if idx in draft:
        draft.remove(idx)
    else:
        draft.append(idx)
        
    await state.update_data(mult_draft=draft)
    
    markup = build_multiple_keyboard(q['id'], q['answers'], draft)
    try:
        await callback.message.edit_reply_markup(reply_markup=markup)
    except Exception:
        pass
    await callback.answer()

@dp.callback_query(F.data.startswith("mulsub_"))
async def handle_mul_submit(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    q = data.get("current_q")
    if not q:
        await callback.answer()
        return
        
    draft = data.get('mult_draft', [])
    if not draft:
        await callback.answer("Пожалуйста, выберите хотя бы один вариант!", show_alert=True)
        return
        
    is_correct = True
    user_texts = []
    correct_indices = [i for i, a in enumerate(q['answers']) if a.get('is_correct')]
    
    if set(draft) != set(correct_indices):
        is_correct = False
        
    for i in sorted(draft):
        user_texts.append(q['answers'][i]['text'])
        
    user_answer_text = "\n".join(user_texts)
    
    await callback.message.edit_reply_markup(reply_markup=None)
    await process_answer(callback.message, callback.from_user.id, state, q, user_answer_text, is_correct)
    await callback.answer()

@dp.callback_query(F.data.startswith("match_"))
async def handle_match_click(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    q = data.get("current_q")
    if not q:
        await callback.answer("Сессия истекла.", show_alert=True)
        return
        
    parts = callback.data.split('_')
    q_id_str, L_key, R_key = parts[1], parts[2], parts[3]
    
    draft = data.get('matching_draft', {})
    r_map = data.get('r_map', {})
    
    # Toggle logic
    if draft.get(L_key) == R_key:
        draft[L_key] = None
    else:
        draft[L_key] = R_key
        
    await state.update_data(matching_draft=draft)
    
    markup = build_matching_keyboard(q['id'], draft, r_map)
    try:
        await callback.message.edit_reply_markup(reply_markup=markup)
    except Exception:
        pass # message unchanged error
    await callback.answer()

@dp.callback_query(F.data.startswith("matchsub_"))
async def handle_match_submit(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    q = data.get("current_q")
    if not q:
        await callback.answer()
        return
        
    draft = data.get('matching_draft', {})
    r_map = data.get('r_map', {})
    L_list = data.get('L_list', [])
    
    # Validation: all must be answered?
    if None in draft.values():
        await callback.answer("Пожалуйста, выберите пару для всех L!", show_alert=True)
        return
        
    # Check if correct
    is_correct = True
    user_answer_text_parts = []
    
    for i, L_text in enumerate(L_list):
        L_k = f"L{i+1}"
        ans_obj = next((a for a in q['answers'] if a['text'] == L_text), None)
        correct_R = ans_obj['match']
        
        chosen_R_idx = draft[L_k]
        chosen_R_text = r_map[chosen_R_idx]
        user_answer_text_parts.append(f"{L_k} - {chosen_R_text}")
        
        if chosen_R_text != correct_R:
            is_correct = False
            
    user_answer_text = ", ".join(user_answer_text_parts)
    
    await callback.message.edit_reply_markup(reply_markup=None) # remove keyboard
    await process_answer(callback.message, callback.from_user.id, state, q, user_answer_text, is_correct)
    await callback.answer()

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await message.answer("Привет! Я бот для подготовки по экономике. Выбери действие:", reply_markup=get_main_keyboard())
    await state.clear()

@dp.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext):
    str_id = str(message.from_user.id)
    if str_id in sessions:
        sessions[str_id] = {
            "answered": [],
            "last_level": None,
            "score": 0,
            "asked": 0,
            "errors": {}
        }
        save_sessions(sessions)
    await message.answer("Ваш прогресс и ошибки были полностью сброшены! Вы начинаете с 1-го вопроса нового блока.", reply_markup=get_main_keyboard())
    await state.clear()

@dp.message(F.text == "📊 Статистика")
async def cmd_stats(message: Message):
    session = get_session(message.from_user.id)
    total_db = len(questions)
    passed = len(session.get("answered", []))
    errors_total = len(session.get("errors", {}).keys())
    left = total_db - passed
    
    stats_text = (
        f"📊 *Ваша Статистика:*\n\n"
        f"🏆 Опыт (XP): {session['xp']}\n"
        f"🔥 Ударный режим: {session['streak']} дней\n\n"
        f"Всего вопросов пройдено (впервые): {passed} из {total_db}\n"
        f"Текущих неисправленных ошибок: {errors_total}\n"
        f"Осталось нерешенных в базе: {left}\n"
    )
    
    # Generate Pie Chart
    labels = ['Пройдено', 'Осталось']
    sizes = [passed, left]
    colors = ['#4CAF50', '#E0E0E0']
    
    fig, ax = plt.subplots(figsize=(5, 5))
    ax.pie(sizes, labels=labels, colors=colors, autopct='%1.1f%%', startangle=90, textprops={'fontsize': 12})
    ax.axis('equal')
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', transparent=True)
    buf.seek(0)
    
    # Workaround for AIOGram in-memory bytes
    from aiogram.types import BufferedInputFile
    photo = BufferedInputFile(buf.getvalue(), filename="stats.png")
    
    await message.answer_photo(photo=photo, caption=stats_text, parse_mode="Markdown")
    plt.close(fig)

@dp.message(F.text == "Решать 60 вопросов")
async def cmd_train(message: Message, state: FSMContext):
    session = get_session(message.from_user.id)
    if not get_next_question(session):
        await message.answer("Вы уже прошли все вопросы во всех блоках базы!", reply_markup=get_main_keyboard())
        return
        
    await state.update_data(mode="normal")
    await message.answer("Начинаем тренировку! 60 вопросов.", reply_markup=get_main_keyboard())
    await send_question(message, message.from_user.id, state, count_for_today=60)

@dp.message(F.text == "Работа над ошибками")
async def cmd_errors(message: Message, state: FSMContext):
    session = get_session(message.from_user.id)
    if not session.get("errors"):
        await message.answer("У вас пока нет ошибок для отработки! Вы молодец 🎉", reply_markup=get_main_keyboard())
        return
        
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    errors_to_practice = list(session["errors"].keys())
            
    if not errors_to_practice:
        await message.answer("На сегодня все ошибки отработаны! Отличная работа 🧠", reply_markup=get_main_keyboard())
        return
        
    limit = min(60, len(errors_to_practice))
    
    await state.update_data(mode="errors", error_queue=errors_to_practice[:limit])
    await message.answer(f"Начинаем работу над ошибками. Заданий на сегодня: {limit}.", reply_markup=get_main_keyboard())
    await send_question(message, message.from_user.id, state, count_for_today=limit)

@dp.message(F.text == "🎓 Симуляция экзамена")
@dp.message(Command("exam"))
async def cmd_exam(message: Message, state: FSMContext):
    session = get_session(message.from_user.id)
    answered = session.get("answered", [])
    if len(answered) < 5:
        await message.answer("Для экзамена нужно решить хотя бы 5 вопросов в режиме тренировки!", reply_markup=get_main_keyboard())
        return
        
    limit = min(20, len(answered))
    exam_q_ids = random.sample(answered, limit)
    
    await state.update_data(mode="exam", exam_queue=exam_q_ids, exam_score=0, exam_total=limit)
    await message.answer(f"🎓 Начинаем симуляцию экзамена! Вопросов: {limit}. Удачи!", reply_markup=get_main_keyboard())
    await send_question(message, message.from_user.id, state, count_for_today=limit)

@dp.callback_query(F.data.startswith("ans_"))
async def handle_inline_answer(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    q = data.get("current_q")
    if not q:
        await callback.answer("Сессия истекла.", show_alert=True)
        return
        
    await state.update_data(current_q=None) # Block double clicks
    
    parts = callback.data.split('_')
    q_id_str, ans_idx_str = parts[1], parts[2]
    ans_idx = int(ans_idx_str)
    
    selected_answer = q['answers'][ans_idx]
    is_correct = selected_answer['is_correct']
    
    await callback.message.edit_reply_markup(reply_markup=None) # remove keyboard
    await process_answer(callback.message, callback.from_user.id, state, q, selected_answer['text'], is_correct)
    await callback.answer()

@dp.message(F.text, QuizState.answering)
async def handle_text_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    q = data.get("current_q")
    if not q:
        return
        
    await state.update_data(current_q=None) # Anti-double click prevention
    
    user_text = message.text.strip().lower()
    correct_answers = [ans['text'].strip().lower() for ans in q['answers'] if ans.get('is_correct')]
    is_correct = False
    
    for c_ans in correct_answers:
        if user_text == c_ans or user_text in c_ans or c_ans in user_text:
            is_correct = True
            break
            
    await process_answer(message, message.from_user.id, state, q, user_text, is_correct)

async def process_answer(message: Message, user_id: int, state: FSMContext, q, user_answer_text, is_correct):
    session = get_session(user_id)
    data = await state.get_data()
    mode = data.get("mode", "normal")
    q_id_str = str(q['id'])
    
    # Process Streak
    today_dt = datetime.now()
    today_str = today_dt.strftime("%Y-%m-%d")
    last_active = session.get("last_active_date", "")
    
    if last_active != today_str:
        if last_active:
            last_dt = datetime.strptime(last_active, "%Y-%m-%d")
            diff = (today_dt.date() - last_dt.date()).days
            if diff <= 2:
                session["streak"] += 1
            else:
                session["streak"] = 1
        else:
            session["streak"] = 1
        session["last_active_date"] = today_str

    if mode == "normal":
        session["asked"] += 1
        if is_correct:
            session["score"] += 1
            session["xp"] += 10
        else:
            if q_id_str not in session["errors"]:
                session["errors"][q_id_str] = "" 
            # Add to spaced repetition
            if q_id_str not in session["spaced_repetition"]:
                session["spaced_repetition"][q_id_str] = {"consecutive_correct": 0, "next_review_date": (today_dt + timedelta(days=2)).strftime("%Y-%m-%d")}
                
    elif mode == "exam":
        exam_queue = data.get("exam_queue", [])
        if exam_queue:
            exam_queue.pop(0)
            await state.update_data(exam_queue=exam_queue)
            
        if is_correct:
            exam_score = data.get("exam_score", 0) + 1
            await state.update_data(exam_score=exam_score)
            
        await bot.send_message(chat_id=user_id, text="Ответ записан ✍️")
        q_left = data.get("questions_left_today", 1) - 1
        await state.update_data(questions_left_today=q_left)
        await asyncio.sleep(1)
        await send_question(message, user_id, state, count_for_today=q_left)
        return
        
    else:
        error_queue = data.get("error_queue", [])
        if error_queue:
            error_queue.pop(0) 
            await state.update_data(error_queue=error_queue)
            
        if is_correct:
            session["xp"] += 5
            if q_id_str in session["errors"]:
                del session["errors"][q_id_str]
            if q_id_str in session["spaced_repetition"]:
                del session["spaced_repetition"][q_id_str]
        else:
            # Failed again, keep in errors
            session["xp"] = max(0, session["xp"] - 2) # small penalty
    
    if is_correct:
        save_sessions(sessions)
        await bot.send_message(chat_id=user_id, text="✅ *Верно!*", parse_mode="Markdown")
    else:
        if q['type'] == 'matching':
            correct_list = "\n".join([f"{a['text']} -> {a['match']}" for a in q['answers'] if a.get('match')])
        else:
            correct_list = "\n".join([ans['text'] for ans in q['answers'] if ans.get('is_correct')])
            
        # Check cache early so we can display it all at once if available
        explanation = session["errors"].get(q_id_str, "")
        
        safe_exp = ""
        if explanation:
            safe_exp = explanation.replace('*', '').replace('_', '').replace('`', '')
            await bot.send_message(chat_id=user_id, text=f"❌ *Неверно!*\n\n*Правильный ответ:*\n{correct_list}\n\n*Объяснение AI:*\n{safe_exp}", parse_mode="Markdown")
        else:
            msg = await bot.send_message(chat_id=user_id, text=f"❌ *Неверно!*\n\n*Правильный ответ:*\n{correct_list}\n\n_Генерирую краткое объяснение ИИ..._", parse_mode="Markdown")
            
            try:
                loop = asyncio.get_event_loop()
                explanation = await loop.run_in_executor(None, ask_gemini, q['question'], correct_list, user_answer_text)
                if mode == "normal" or q_id_str in session["errors"]:
                    session["errors"][q_id_str] = explanation
            except Exception as e:
                explanation = f"(Объяснение недоступно: {e})"
                
            safe_exp = explanation.replace('*', '').replace('_', '').replace('`', '')
            await msg.edit_text(f"❌ *Неверно!*\n\n*Правильный ответ:*\n{correct_list}\n\n*Объяснение AI:*\n{safe_exp}", parse_mode="Markdown")

    if mode == "normal":
        if q['id'] not in session.get("answered", []):
            session["answered"].append(q['id'])
        session["last_level"] = q['level_name']
    
    save_sessions(sessions)
    q_left = data.get("questions_left_today", 1) - 1
    await asyncio.sleep(2)
    await send_question(message, user_id, state, count_for_today=q_left)

async def main():
    print("Bot is starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
