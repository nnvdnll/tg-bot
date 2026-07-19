"""
Telegram-бот для любого числа пользователей:
- каждый ставит себе задачи на конкретный день (в пределах ближайшего месяца)
- можно оставить заметку любому другому зарегистрированному пользователю на конкретный день
- каждое утро бот сам присылает каждому его задачи + адресованные ему заметки на сегодня

Запуск: см. README.md
"""

import asyncio
import logging
import os
import sqlite3
from datetime import date, timedelta

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---------- Настройки ----------

TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_СВОЙ_ТОКЕН")
MORNING_HOUR = int(os.getenv("MORNING_HOUR", "8"))
MORNING_MINUTE = int(os.getenv("MORNING_MINUTE", "0"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

DAYS_AHEAD = 30      # на сколько дней вперёд от сегодня можно ставить задачи/заметки
DAYS_PER_PAGE = 7    # сколько дат показывать на одной "странице" клавиатуры

MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

# ---------- Кнопки главного меню ----------

BTN_TASK = "📝 Новая задача"
BTN_NOTE = "💌 Новая заметка"
BTN_TODAY = "📅 Сегодня"
BTN_CANCEL = "❌ Отмена"
BTN_PRODUCTS = "🛒 Продукты"


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TASK), KeyboardButton(text=BTN_NOTE)],
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_PRODUCTS)],  # ← добавили BTN_PRODUCTS сюда
        ],
        resize_keyboard=True,
    )


def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
    )

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ---------- База данных ----------

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            name TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            created_by INTEGER,
            task_date TEXT,
            text TEXT,
            done INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_chat_id INTEGER,
            to_chat_id INTEGER,
            note_date TEXT,
            text TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS shopping_list (
            id INTEGER PRIMARY KEY,
            content TEXT
        )"""
    )
    conn.commit()
    conn.close()


def migrate_db() -> None:
    """Добавляет новые колонки в уже существующую базу (если бот запускался раньше)."""
    conn = get_conn()
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN created_by INTEGER")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # колонка уже есть
    conn.close()


def get_conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def add_user(chat_id: int, name: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO users (chat_id, name) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name",
        (chat_id, name),
    )
    conn.commit()
    conn.close()


def is_registered(chat_id: int) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return row is not None


def get_users() -> list[tuple[int, str]]:
    conn = get_conn()
    rows = conn.execute("SELECT chat_id, name FROM users ORDER BY name").fetchall()
    conn.close()
    return rows


def get_other_users(chat_id: int) -> list[tuple[int, str]]:
    return [(uid, name) for uid, name in get_users() if uid != chat_id]


def get_user_name(chat_id: int) -> str:
    conn = get_conn()
    row = conn.execute("SELECT name FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return row[0] if row else "Неизвестный"


def add_task(assignee_id: int, created_by: int, task_date: str, text: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO tasks (chat_id, created_by, task_date, text) VALUES (?, ?, ?, ?)",
        (assignee_id, created_by, task_date, text),
    )
    conn.commit()
    conn.close()


def get_tasks(chat_id: int, task_date: str) -> list[tuple[int, str, int, int]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, text, done, created_by FROM tasks WHERE chat_id=? AND task_date=? ORDER BY id",
        (chat_id, task_date),
    ).fetchall()
    conn.close()
    return rows


def add_note(from_id: int, to_id: int, note_date: str, text: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO notes (from_chat_id, to_chat_id, note_date, text) VALUES (?, ?, ?, ?)",
        (from_id, to_id, note_date, text),
    )
    conn.commit()
    conn.close()


def get_notes_for(to_id: int, note_date: str) -> list[tuple[int, str]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT from_chat_id, text FROM notes WHERE to_chat_id=? AND note_date=? ORDER BY id",
        (to_id, note_date),
    ).fetchall()
    conn.close()
    return rows
def get_shopping_list() -> str | None:
    conn = get_conn()
    row = conn.execute("SELECT content FROM shopping_list WHERE id=1").fetchone()
    conn.close()
    return row[0] if row else None


def set_shopping_list(content: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO shopping_list (id, content) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET content=excluded.content",
        (content,),
    )
    conn.commit()
    conn.close()

# ---------- Выбор даты (страница пересчитывается от сегодняшней даты каждый раз) ----------

def format_day(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


def date_keyboard(prefix: str, page: int = 0) -> InlineKeyboardMarkup:
    """
    Строит клавиатуру с датами на DAYS_PER_PAGE дней, начиная от today() + page*DAYS_PER_PAGE.
    today() вычисляется в момент вызова, так что диапазон дат всегда актуален
    относительно текущего дня, а не "заморожен" на момент старта бота.
    """
    today = date.today()
    start_offset = page * DAYS_PER_PAGE
    day_buttons = []
    for i in range(DAYS_PER_PAGE):
        offset = start_offset + i
        if offset >= DAYS_AHEAD:
            break
        d = today + timedelta(days=offset)
        label = "Сегодня" if offset == 0 else ("Завтра" if offset == 1 else format_day(d))
        day_buttons.append(
            InlineKeyboardButton(text=label, callback_data=f"date_pick:{prefix}:{d.isoformat()}")
        )

    rows = [day_buttons[i:i + 2] for i in range(0, len(day_buttons), 2)]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️ Раньше", callback_data=f"date_page:{prefix}:{page - 1}"))
    if start_offset + DAYS_PER_PAGE < DAYS_AHEAD:
        nav_row.append(InlineKeyboardButton(text="Позже ▶️", callback_data=f"date_page:{prefix}:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


class RegisterForm(StatesGroup):
    entering_name = State()


class TaskForm(StatesGroup):
    choosing_recipient = State()
    choosing_date = State()
    entering_text = State()


class NoteForm(StatesGroup):
    choosing_recipient = State()
    choosing_date = State()
    entering_text = State()

class ProductsForm(StatesGroup):
    entering_list = State()

# ---------- Регистрация ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if is_registered(message.chat.id):
        await message.answer(
            f"С возвращением, {get_user_name(message.chat.id)}!\nВыбери действие на клавиатуре ниже 👇",
            reply_markup=main_menu_kb(),
        )
        return

    await state.set_state(RegisterForm.entering_name)
    await message.answer("Привет! Как тебя зовут? (это имя увидят другие пользователи при выборе получателя заметки)")


@router.message(RegisterForm.entering_name)
async def register_name_entered(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    add_user(message.chat.id, name)
    await state.clear()
    await message.answer(
        f"Отлично, {name}! Теперь ты зарегистрирован(а).\n\n"
        f"Каждое утро в {MORNING_HOUR:02d}:{MORNING_MINUTE:02d} я буду присылать сводку на день сам.\n\n"
        "Выбери действие на клавиатуре ниже 👇",
        reply_markup=main_menu_kb(),
    )


# ---------- Отмена текущего действия ----------

@router.message(F.text == BTN_CANCEL)
async def cancel_action(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb())


# ---------- /task ----------

@router.message(Command("task"))
@router.message(F.text == BTN_TASK)
async def cmd_task(message: Message, state: FSMContext) -> None:
    if not is_registered(message.chat.id):
        await message.answer("Сначала зарегистрируйся: отправь /start")
        return

    others = get_other_users(message.chat.id)
    buttons = [[InlineKeyboardButton(text="Себе", callback_data="task_recipient:me")]]
    buttons += [
        [InlineKeyboardButton(text=name, callback_data=f"task_recipient:{uid}")]
        for uid, name in others
    ]
    await state.set_state(TaskForm.choosing_recipient)
    await message.answer("Кому задача?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(TaskForm.choosing_recipient, F.data.startswith("task_recipient:"))
async def task_recipient_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    raw = callback.data.split(":", 1)[1]
    assignee_id = callback.from_user.id if raw == "me" else int(raw)
    await state.update_data(assignee_id=assignee_id)
    await state.set_state(TaskForm.choosing_date)

    label = "себе" if raw == "me" else get_user_name(assignee_id)
    await callback.message.edit_text(f"Задача для: {label}.\nНа какой день?")
    await callback.message.edit_reply_markup(reply_markup=date_keyboard("task"))
    await callback.answer()


@router.callback_query(TaskForm.choosing_date, F.data.startswith("date_page:task:"))
async def task_date_page(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":")[-1])
    await callback.message.edit_reply_markup(reply_markup=date_keyboard("task", page))
    await callback.answer()


@router.callback_query(TaskForm.choosing_date, F.data.startswith("date_pick:task:"))
async def task_date_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    chosen_date = callback.data.split(":", 2)[2]
    await state.update_data(task_date=chosen_date)
    await state.set_state(TaskForm.entering_text)
    await callback.message.edit_text(f"Ок, дата: {chosen_date}\nТеперь напиши текст задачи.")
    await callback.message.answer("Введи текст ниже:", reply_markup=cancel_kb())
    await callback.answer()


@router.message(TaskForm.entering_text)
async def task_text_entered(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    add_task(data["assignee_id"], message.chat.id, data["task_date"], message.text)
    await state.clear()
    if data["assignee_id"] == message.chat.id:
        reply = f"Задача на {data['task_date']} добавлена ✅"
    else:
        reply = f"Задача для {get_user_name(data['assignee_id'])} на {data['task_date']} отправлена ✅"
        try:
            await bot.send_message(
                data["assignee_id"],
                f"📝 Новая задача от {get_user_name(message.chat.id)} на {data['task_date']}:\n{message.text}",
            )
        except Exception:
            logging.exception("Не смог уведомить пользователя %s о новой задаче", data["assignee_id"])
    await message.answer(reply, reply_markup=main_menu_kb())


# ---------- /note ----------

@router.message(Command("note"))
@router.message(F.text == BTN_NOTE)
async def cmd_note(message: Message, state: FSMContext) -> None:
    if not is_registered(message.chat.id):
        await message.answer("Сначала зарегистрируйся: отправь /start")
        return

    others = get_other_users(message.chat.id)
    if not others:
        await message.answer("Пока что больше никто не зарегистрирован в боте.", reply_markup=main_menu_kb())
        return

    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"note_recipient:{uid}")]
        for uid, name in others
    ]
    await state.set_state(NoteForm.choosing_recipient)
    await message.answer("Кому заметка?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(NoteForm.choosing_recipient, F.data.startswith("note_recipient:"))
async def note_recipient_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    recipient_id = int(callback.data.split(":", 1)[1])
    await state.update_data(recipient_id=recipient_id)
    await state.set_state(NoteForm.choosing_date)
    await callback.message.edit_text(
        f"Заметка для {get_user_name(recipient_id)}.\nНа какой день?"
    )
    await callback.message.edit_reply_markup(reply_markup=date_keyboard("note"))
    await callback.answer()


@router.callback_query(NoteForm.choosing_date, F.data.startswith("date_page:note:"))
async def note_date_page(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":")[-1])
    await callback.message.edit_reply_markup(reply_markup=date_keyboard("note", page))
    await callback.answer()


@router.callback_query(NoteForm.choosing_date, F.data.startswith("date_pick:note:"))
async def note_date_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    chosen_date = callback.data.split(":", 2)[2]
    await state.update_data(note_date=chosen_date)
    await state.set_state(NoteForm.entering_text)
    await callback.message.edit_text(f"Ок, дата: {chosen_date}\nТеперь напиши текст заметки.")
    await callback.message.answer("Введи текст ниже:", reply_markup=cancel_kb())
    await callback.answer()


@router.message(NoteForm.entering_text)
async def note_text_entered(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    add_note(message.chat.id, data["recipient_id"], data["note_date"], message.text)
    await state.clear()
    recipient_name = get_user_name(data["recipient_id"])
    await message.answer(f"Заметка для {recipient_name} на {data['note_date']} сохранена ✅", reply_markup=main_menu_kb())
    try:
        await bot.send_message(
            data["recipient_id"],
            f"💌 Новая заметка от {get_user_name(message.chat.id)} на {data['note_date']}:\n{message.text}",
        )
    except Exception:
        logging.exception("Не смог уведомить пользователя %s о новой заметке", data["recipient_id"])

# ---------- /today ----------
# ---------- 🛒 Продукты ----------

@router.message(F.text == BTN_PRODUCTS)
async def cmd_products(message: Message, state: FSMContext) -> None:
    if not is_registered(message.chat.id):
        await message.answer("Сначала зарегистрируйся: отправь /start")
        return

    current = get_shopping_list()
    if current:
        await message.answer(f"🛒 Текущий список продуктов:\n\n{current}")
    else:
        await message.answer("Список продуктов пока пуст.")

    await state.set_state(ProductsForm.entering_list)
    await message.answer(
        "Пришли список продуктов (можно в несколько строк) — он заменит текущий и будет виден всем.",
        reply_markup=cancel_kb(),
    )


@router.message(ProductsForm.entering_list)
async def products_list_entered(message: Message, state: FSMContext) -> None:
    set_shopping_list(message.text)
    await state.clear()
    await message.answer("Список продуктов обновлён ✅", reply_markup=main_menu_kb())

def build_digest(chat_id: int, for_date: str) -> str:
    tasks = get_tasks(chat_id, for_date)
    notes = get_notes_for(chat_id, for_date)

    lines = [f"📅 Сводка на {for_date}"]

    lines.append("\n📝 Твои задачи:")
    if tasks:
        for _id, text, done, created_by in tasks:
            mark = "✅" if done else "▫️"
            if created_by is not None and created_by != chat_id:
                lines.append(f"{mark} {text} (от {get_user_name(created_by)})")
            else:
                lines.append(f"{mark} {text}")
    else:
        lines.append("(задач нет)")

    lines.append("\n💌 Заметки от других:")
    if notes:
        for from_id, text in notes:
            lines.append(f"— {get_user_name(from_id)}: {text}")
    else:
        lines.append("(заметок нет)")

    return "\n".join(lines)


@router.message(Command("today"))
@router.message(F.text == BTN_TODAY)
async def cmd_today(message: Message) -> None:
    if not is_registered(message.chat.id):
        await message.answer("Сначала зарегистрируйся: отправь /start")
        return
    today_str = date.today().isoformat()
    await message.answer(build_digest(message.chat.id, today_str), reply_markup=main_menu_kb())


# ---------- Утренняя рассылка ----------

async def send_morning_digest() -> None:
    today_str = date.today().isoformat()
    for chat_id, _name in get_users():
        text = "Доброе утро! ☀️\n\n" + build_digest(chat_id, today_str)
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            logging.exception("Не смог отправить утреннюю сводку пользователю %s", chat_id)


# ---------- Запуск ----------

async def main() -> None:
    init_db()
    migrate_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        send_morning_digest,
        trigger="cron",
        hour=MORNING_HOUR,
        minute=MORNING_MINUTE,
    )
    scheduler.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
