import telebot
from telebot import types
import sqlite3
import os
import re
from datetime import datetime, timedelta
import threading
import time
import dateparser
import re
import uuid

API_TOKEN = os.getenv("BOT_TOKEN") or ""
bot = telebot.TeleBot(API_TOKEN, parse_mode='HTML')
calendar_data = {}
add_member_state = {}

# === DB ===
conn = sqlite3.connect("task_bot.db", check_same_thread=False)
c = conn.cursor()

# Таблица пользователей
c.execute('''
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE,
    username TEXT
)
''')
c.execute('''
CREATE TABLE IF NOT EXISTS group_invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER,
    code TEXT UNIQUE,
    used INTEGER DEFAULT 0
)
''')
conn.commit()

# Таблица групп
c.execute('''
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    owner_id INTEGER,
    FOREIGN KEY (owner_id) REFERENCES users (id)
)
''')

# Таблица участников групп
c.execute('''
CREATE TABLE IF NOT EXISTS group_members (
    user_id INTEGER,
    group_id INTEGER,
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (group_id) REFERENCES groups (id),
    PRIMARY KEY (user_id, group_id)
)
''')

# Таблица задач
c.execute('''
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    importance INTEGER CHECK (importance BETWEEN 1 AND 5),
    start_time TEXT,
    end_time TEXT,
    is_done INTEGER DEFAULT 0,
    user_id INTEGER,
    group_id INTEGER,
    remind_2d INTEGER DEFAULT 0,
    remind_1d INTEGER DEFAULT 0,
    remind_1h INTEGER DEFAULT 0,
    repeat_interval TEXT, -- значения: 'hourly', 'daily', 'weekly', 'monthly', 'yearly' или NULL
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (group_id) REFERENCES groups (id)
)
''')

try:
    c.execute("ALTER TABLE tasks ADD COLUMN repeat_interval TEXT")
    conn.commit()
except sqlite3.OperationalError:
    pass
conn.commit()


def notification_worker():
    while True:
        now = datetime.now()

        # Проверка всех задач, у которых уведомления ещё не отправлены
        c.execute("""
            SELECT id, title, start_time, user_id
            FROM tasks
            WHERE is_done = 0 AND user_id IS NOT NULL
        """)
        tasks = c.fetchall()

        for task_id, title, start_str, user_id in tasks:
            try:
                start_time = datetime.fromisoformat(start_str)
            except Exception:
                continue

            delta = start_time - now

            notify = None
            field = None

            if timedelta(hours=1) <= delta < timedelta(hours=1, minutes=1):
                field = "remind_1h"
                notify = "⏰ Через 1 час начнётся задача"
            elif timedelta(days=1) <= delta < timedelta(days=1, minutes=1):
                field = "remind_1d"
                notify = "📅 Завтра начнётся задача"
            elif timedelta(days=2) <= delta < timedelta(days=2, minutes=1):
                field = "remind_2d"
                notify = "🗓 Через 2 дня начнётся задача"

            if field and notify:
                c.execute(f"SELECT {field} FROM tasks WHERE id = ?", (task_id,))
                already_sent = c.fetchone()[0]
                if not already_sent:
                    # Получаем telegram_id
                    c.execute("SELECT telegram_id FROM users WHERE id = ?", (user_id,))
                    row = c.fetchone()
                    if row:
                        tg_id = row[0]
                        bot.send_message(tg_id, f"{notify}: <b>{title}</b>", parse_mode="HTML")
                        c.execute(f"UPDATE tasks SET {field} = 1 WHERE id = ?", (task_id,))
                        conn.commit()

        time.sleep(60)


def handle_group_join_by_code(message, code):
    telegram_id = message.from_user.id
    username = message.from_user.username or ''

    # Проверка кода
    c.execute("SELECT group_id, used FROM group_invites WHERE code = ?", (code,))
    row = c.fetchone()
    if not row:
        bot.send_message(message.chat.id, "❌ Ссылка недействительна.")
        return
    group_id, used = row

    if used:
        bot.send_message(message.chat.id, "❌ Ссылка уже использована.")
        return

    # Получаем user_id
    c.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
    user_row = c.fetchone()
    if not user_row:
        c.execute("INSERT INTO users (telegram_id, username) VALUES (?, ?)", (telegram_id, username))
        user_id = c.lastrowid
    else:
        user_id = user_row[0]

    # Добавляем в группу
    c.execute("SELECT 1 FROM group_members WHERE user_id = ? AND group_id = ?", (user_id, group_id))
    if c.fetchone():
        bot.send_message(message.chat.id, "ℹ️ Вы уже состоите в этой группе.")
        return

    c.execute("INSERT INTO group_members (user_id, group_id) VALUES (?, ?)", (user_id, group_id))
    c.execute("UPDATE group_invites SET used = 1 WHERE code = ?", (code,))
    conn.commit()

    bot.send_message(message.chat.id, "✅ Вы успешно присоединились к группе!")
    send_main_menu(message.chat.id)

def process_forwarded_member(message):
    from_user = message.from_user
    forward = message.forward_from
    adder_id = message.from_user.id

    if not forward:
        bot.send_message(message.chat.id, "⚠️ Пожалуйста, перешлите именно сообщение пользователя.")
        return

    forward_id = forward.id
    forward_telegram_id = forward.id
    forward_username = forward.username or ""

    group_id = add_member_state.get(adder_id)
    if group_id is None:
        bot.send_message(message.chat.id, "⚠️ Группа не определена. Попробуйте снова.")
        return

    # Проверяем, зарегистрирован ли forward_user
    c.execute("SELECT id FROM users WHERE telegram_id = ?", (forward_telegram_id,))
    user_row = c.fetchone()

    if not user_row:
        # Если нет — зарегистрируем
        c.execute("INSERT INTO users (telegram_id, username) VALUES (?, ?)", (forward_telegram_id, forward_username))
        user_id = c.lastrowid
    else:
        user_id = user_row[0]

    # Проверим, не состоит ли уже в группе
    c.execute("SELECT 1 FROM group_members WHERE user_id = ? AND group_id = ?", (user_id, group_id))
    if c.fetchone():
        bot.send_message(message.chat.id, "ℹ️ Этот пользователь уже находится в группе.")
        return

    # Добавим участника
    c.execute("INSERT INTO group_members (user_id, group_id) VALUES (?, ?)", (user_id, group_id))
    conn.commit()

    bot.send_message(message.chat.id, f"✅ Пользователь <b>@{forward_username}</b> добавлен в группу!", parse_mode="HTML")


def save_group_task(message, state):
    telegram_id = message.from_user.id
    title = state["title"]
    description = state["description"]
    repeat = state.get("repeat_interval")
    start_time = state.get("start_time", datetime.now().isoformat())
    group_id = state["group_id"]

    # Получаем user_id
    c.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
    user_row = c.fetchone()
    if not user_row:
        bot.send_message(message.chat.id, "❌ Ошибка: пользователь не найден.")
        return
    user_id = user_row[0]

    if state.get("edit_mode"):
        task_id = state["task_id"]
        c.execute("""
            UPDATE tasks
            SET title = ?, description = ?, start_time = ?, repeat_interval = ?
            WHERE id = ?
        """, (title, description, start_time, repeat, task_id))
    else:
        c.execute("""
            INSERT INTO tasks (title, description, group_id, user_id, repeat_interval, start_time)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (title, description, group_id, user_id, repeat, start_time))

    conn.commit()
    del task_creation_state[telegram_id]

    msg = "✏️ Задача успешно обновлена!" if state.get("edit_mode") else f"✅ Задача <b>{title}</b> успешно создана!"
    bot.send_message(message.chat.id, msg, parse_mode="HTML")
    send_main_menu(message.chat.id)


def send_main_menu(chat_id, text="📋 Главное меню"):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn_tasks = types.KeyboardButton("📋 Мои задачи")
    btn_new = types.KeyboardButton("🆕 Новая задача")
    btn_groups = types.KeyboardButton("👥 Группы")
    btn_settings = types.KeyboardButton("⚙️ Настройки")
    markup.add(btn_tasks, btn_new, btn_groups, btn_settings)
    bot.send_message(chat_id, text, reply_markup=markup)


def register_user(message):
    telegram_id = message.from_user.id
    username = message.from_user.username or ''
    c.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
    if not c.fetchone():
        c.execute("INSERT INTO users (telegram_id, username) VALUES (?, ?)", (telegram_id, username))
        conn.commit()

def process_group_name(message):
    group_name = message.text.strip()
    telegram_id = message.from_user.id

    # Получим ID пользователя
    c.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
    user_row = c.fetchone()
    if not user_row:
        bot.send_message(message.chat.id, "❌ Ошибка: пользователь не найден.")
        return
    user_id = user_row[0]

    # Проверим, существует ли группа с таким именем
    c.execute("SELECT id FROM groups WHERE name = ?", (group_name,))
    if c.fetchone():
        bot.send_message(message.chat.id, "⚠️ Группа с таким названием уже существует. Попробуйте другое имя.")
        return

    # Добавим в таблицу groups
    c.execute("INSERT INTO groups (name, owner_id) VALUES (?, ?)", (group_name, user_id))
    group_id = c.lastrowid

    # Автоматически добавим владельца как участника
    c.execute("INSERT INTO group_members (user_id, group_id) VALUES (?, ?)", (user_id, group_id))
    conn.commit()

    bot.send_message(message.chat.id, f"✅ Группа <b>{group_name}</b> успешно создана и вы назначены владельцем!")
    send_main_menu(message.chat.id)

### Handlers ###



@bot.callback_query_handler(func=lambda call: call.data.startswith("add_member_"))
def callback_add_member_link(call):
    group_id = int(call.data.split("_")[2])
    bot_username = bot.get_me().username

    # Генерируем уникальный код
    code = str(uuid.uuid4())[:8]

    # Сохраняем код
    c.execute("INSERT INTO group_invites (group_id, code) VALUES (?, ?)", (group_id, code))
    conn.commit()

    link = f"https://t.me/{bot_username}?start=join_group_{code}"
    bot.send_message(call.message.chat.id, f"🔗 Отправьте пользователю эту ссылку для вступления в группу:\n{link}")


@bot.message_handler(commands=['start'])
def handle_start(message):
    register_user(message)

    args = message.text.split()
    if len(args) == 2 and args[1].startswith("join_group_"):
        code = args[1].replace("join_group_", "")
        handle_group_join_by_code(message, code)
    else:
        send_main_menu(message.chat.id, "👋 Привет! Я твой планировщик задач. Выбери действие:")

@bot.message_handler(func=lambda message: message.text == "👥 Группы")
def handle_groups_menu(message):
    markup = types.InlineKeyboardMarkup()
    btn_create = types.InlineKeyboardButton("➕ Создать новую группу", callback_data="create_group")
    btn_list = types.InlineKeyboardButton("✅ Мои группы", callback_data="list_groups_0")
    markup.add(btn_create)
    markup.add(btn_list)
    bot.send_message(message.chat.id, "👥 Управление группами:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "create_group")
def callback_create_group(call):
    msg = bot.send_message(call.message.chat.id, "📝 Введите название новой группы:")
    bot.register_next_step_handler(msg, process_group_name)

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    register_user(message)

    if message.text == "📋 Мои задачи":
      telegram_id = message.from_user.id
      c.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
      user_row = c.fetchone()
      if not user_row:
          bot.send_message(message.chat.id, "❌ Пользователь не найден.")
          return
      user_id = user_row[0]

      c.execute("""
          SELECT id, title, is_done, start_time
          FROM tasks
          WHERE user_id = ? AND group_id IS NULL
          ORDER BY is_done, start_time
      """, (user_id,))
      tasks = c.fetchall()

      if not tasks:
          bot.send_message(message.chat.id, "📭 У вас нет личных задач.")
          return

      markup = types.InlineKeyboardMarkup()
      text = "<b>📋 Ваши личные задачи:</b>\n\n"

      for tid, title, is_done, start in tasks:
          status = "✅" if is_done else "⏳"
          try:
              dt = datetime.fromisoformat(start)
              start_str = dt.strftime('%H:%M %d-%m-%Y')
          except:
              start_str = "??"
          button_text = f"{status} {title} ({start_str})"
          markup.add(types.InlineKeyboardButton(button_text, callback_data=f"/mytask_{tid}"))

      bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")

    elif message.text == "🆕 Новая задача":
      telegram_id = message.from_user.id
      task_creation_state[telegram_id] = {"group_id": None, "step": "title"}

      msg = bot.send_message(message.chat.id, "📝 Введите название задачи:")
      bot.register_next_step_handler(msg, handle_task_creation_step)


    elif message.text == "👥 Группы":
        bot.send_message(message.chat.id, "👥 Работа с группами:")
        # TODO: показать меню групп

    elif message.text == "⚙️ Настройки":
        bot.send_message(message.chat.id, "⚙️ Раздел настроек пока в разработке.")

    else:
        send_main_menu(message.chat.id, "❓ Неизвестная команда. Выберите действие из меню.")

GROUPS_PER_PAGE = 5

@bot.callback_query_handler(func=lambda call: call.data.startswith("/mytask_"))
def callback_mytask_details(call):
    task_id = int(call.data.split("_")[1])

    c.execute("""
        SELECT title, description, start_time, end_time, is_done
        FROM tasks
        WHERE id = ? AND group_id IS NULL
    """, (task_id,))
    task = c.fetchone()

    if not task:
        bot.answer_callback_query(call.id, "Задача не найдена.")
        return

    title, description, start, end, is_done = task
    status = "✅ Выполнена" if is_done else "⏳ В процессе"

    def fmt(t):
        try:
            return datetime.fromisoformat(t).strftime('%H:%M %d-%m-%Y')
        except:
            return "—"

    start_str = fmt(start)
    end_str = fmt(end) if end else ""

    text = (
        f"<b>📝 Задача:</b> {title}\n"
        f"<b>💬 Описание:</b> {description}\n"
        f"<b>🕒 Начало:</b> {start_str}\n"
        f"<b>⏱ Статус:</b> {status}"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✏️ Изменить", callback_data=f"edit_mytask_{task_id}"))
    markup.add(types.InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_mytask_{task_id}"))

    bot.edit_message_text(
        text=text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup,
        parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_mytask_"))
def callback_delete_mytask(call):
    task_id = int(call.data.split("_")[2])
    c.execute("DELETE FROM tasks WHERE id = ? AND group_id IS NULL", (task_id,))
    conn.commit()

    bot.edit_message_text(
        text="🗑 Задача удалена.",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_mytask_"))
def callback_edit_mytask(call):
    task_id = int(call.data.split("_")[2])
    telegram_id = call.from_user.id

    # Загружаем задачу
    c.execute("SELECT user_id, title, description FROM tasks WHERE id = ? AND group_id IS NULL", (task_id,))
    task = c.fetchone()
    if not task:
        bot.answer_callback_query(call.id, "Задача не найдена.")
        return

    _, title, description = task

    task_creation_state[telegram_id] = {
        "group_id": None,
        "task_id": task_id,
        "step": "title",
        "edit_mode": True
    }

    msg = bot.send_message(call.message.chat.id, "✏️ Введите новое название задачи:")
    bot.register_next_step_handler(msg, handle_task_creation_step)


@bot.callback_query_handler(func=lambda call: call.data.startswith("list_groups_"))
def callback_list_groups(call):
    telegram_id = call.from_user.id
    page = int(call.data.split("_")[-1])

    # Получим ID пользователя
    c.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
    row = c.fetchone()
    if not row:
        bot.answer_callback_query(call.id, "Пользователь не найден.")
        return
    user_id = row[0]

    # Получим все группы пользователя
    c.execute("""
        SELECT g.id, g.name, g.owner_id = ? AS is_owner
        FROM groups g
        JOIN group_members gm ON g.id = gm.group_id
        WHERE gm.user_id = ?
        ORDER BY g.name
    """, (user_id, user_id))
    all_groups = c.fetchall()

    if not all_groups:
        bot.edit_message_text(
            "❌ У вас нет групп.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
        return

    # Пагинация
    total = len(all_groups)
    start = page * GROUPS_PER_PAGE
    end = start + GROUPS_PER_PAGE
    current_groups = all_groups[start:end]

    text = f"<b>📋 Ваши группы (страница {page + 1})</b>\n\n"
    markup = types.InlineKeyboardMarkup(row_width=1)

    # Кнопки групп
    for gid, name, is_owner in current_groups:
        owner_label = "👑 " if is_owner else ""
        button_text = f"{owner_label}{name}"
        markup.add(types.InlineKeyboardButton(button_text, callback_data=f"group_{gid}"))

    # Кнопки пагинации
    pagination_buttons = []
    if start > 0:
        pagination_buttons.append(types.InlineKeyboardButton("◀️", callback_data=f"list_groups_{page - 1}"))
    if end < total:
        pagination_buttons.append(types.InlineKeyboardButton("▶️", callback_data=f"list_groups_{page + 1}"))
    if pagination_buttons:
        markup.row(*pagination_buttons)

    bot.edit_message_text(
        text=text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup,
        parse_mode="HTML"
    )


task_creation_state = {}

@bot.callback_query_handler(func=lambda call: call.data.startswith("add_member_"))
def callback_add_member_start(call):
    group_id = int(call.data.split("_")[2])
    telegram_id = call.from_user.id

    add_member_state[telegram_id] = group_id
    msg = bot.send_message(call.message.chat.id, "📨 Перешлите сообщение от пользователя, которого хотите добавить в группу.")
    bot.register_next_step_handler(msg, process_forwarded_member)


@bot.callback_query_handler(func=lambda call: call.data.startswith("group_addtask_"))
def callback_add_task_step1(call):
    group_id = int(call.data.split("_")[2])
    telegram_id = call.from_user.id

    task_creation_state[telegram_id] = {"group_id": group_id, "step": "title"}
    msg = bot.send_message(call.message.chat.id, "📝 Введите название задачи:")
    bot.register_next_step_handler(msg, handle_task_creation_step)

def handle_task_creation_step(message):
    telegram_id = message.from_user.id
    state = task_creation_state.get(telegram_id)
    if not state:
        bot.send_message(message.chat.id, "⚠️ Что-то пошло не так. Начните заново.")
        return

    step = state["step"]

    if step == "title":
        state["title"] = message.text.strip()
        state["step"] = "description"
        msg = bot.send_message(message.chat.id, "💬 Введите описание задачи:")
        bot.register_next_step_handler(msg, handle_task_creation_step)

    elif step == "description":
        state["description"] = message.text.strip()
        state["step"] = "periodic"
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        markup.add("Да", "Нет")
        msg = bot.send_message(message.chat.id, "🔁 Задача будет повторяться?", reply_markup=markup)
        bot.register_next_step_handler(msg, handle_task_creation_step)

    elif step == "periodic":
        answer = message.text.lower()
        if answer == "да":
            state["periodic"] = True
            state["step"] = "period_type"
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            markup.add("hourly", "daily", "weekly", "monthly", "yearly", "через X дней")
            msg = bot.send_message(message.chat.id, "🔂 Как часто повторять?", reply_markup=markup)
            bot.register_next_step_handler(msg, handle_task_creation_step)
        else:
            state["periodic"] = False
            state["step"] = "start_time"
            msg = bot.send_message(message.chat.id, "🕒 Укажите дату начала (например: завтра, 15 июля, 14:00):")
            bot.register_next_step_handler(msg, handle_task_creation_step)

    elif step == "period_type":
        period = message.text.lower()
        if period == "через x дней":
            state["step"] = "custom_period"
            msg = bot.send_message(message.chat.id, "🔢 Введите число дней между повторами:")
            bot.register_next_step_handler(msg, handle_task_creation_step)
        elif period in ["hourly", "daily", "weekly", "monthly", "yearly"]:
            state["repeat_interval"] = period
            save_group_task(message, state)
        else:
            msg = bot.send_message(message.chat.id, "⚠️ Неверный вариант. Повторите выбор.")
            bot.register_next_step_handler(msg, handle_task_creation_step)

    elif step == "custom_period":
        try:
            days = int(message.text.strip())
            state["repeat_interval"] = f"{days}_days"
            save_group_task(message, state)
        except ValueError:
            msg = bot.send_message(message.chat.id, "Введите число, например: 10")
            bot.register_next_step_handler(msg, handle_task_creation_step)

    elif step == "start_time":
        parsed_time = dateparser.parse(message.text)
        if parsed_time:
            state["start_time"] = parsed_time.isoformat()
            save_group_task(message, state)
        else:
            msg = bot.send_message(message.chat.id, "⏳ Не удалось распознать дату. Попробуйте ещё раз:")
            bot.register_next_step_handler(msg, handle_task_creation_step)

@bot.callback_query_handler(func=lambda call: re.fullmatch(r"group_\d+", call.data))
def callback_group_details(call):
    group_id = int(call.data.split("_")[1])
    telegram_id = call.from_user.id

    # Получаем ID пользователя
    c.execute("SELECT id, username FROM users WHERE telegram_id = ?", (telegram_id,))
    user_row = c.fetchone()
    if not user_row:
        bot.answer_callback_query(call.id, "Пользователь не найден.")
        return
    user_id = user_row[0]

    # Получаем инфу о группе
    c.execute("SELECT name, owner_id FROM groups WHERE id = ?", (group_id,))
    group = c.fetchone()
    if not group:
        bot.answer_callback_query(call.id, "Группа не найдена.")
        return
    group_name, owner_id = group

    # Получаем количество участников
    c.execute("SELECT COUNT(*) FROM group_members WHERE group_id = ?", (group_id,))
    member_count = c.fetchone()[0]

    is_owner = user_id == owner_id

    # Формируем сообщение
    text = (
        f"<b>👥 Группа:</b> {group_name}\n"
        f"<b>👑 Владелец:</b> {'Вы' if is_owner else 'ID ' + str(owner_id)}\n"
        f"<b>👤 Участников:</b> {member_count}\n"
    )

    # Кнопки управления
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📋 Задачи группы", callback_data=f"group_tasks_{group_id}"))
    markup.add(types.InlineKeyboardButton("👤 Участники", callback_data=f"group_members_{group_id}"))
    markup.add(types.InlineKeyboardButton("➕ Добавить задачу", callback_data=f"group_addtask_{group_id}"))
    if is_owner:
        markup.add(types.InlineKeyboardButton("❌ Удалить группу", callback_data=f"delete_group_{group_id}"))

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        reply_markup=markup,
        parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("group_tasks_"))
def callback_group_tasks(call):
    group_id = int(call.data.split("_")[2])

    # Получаем название группы
    c.execute("SELECT name FROM groups WHERE id = ?", (group_id,))
    group_row = c.fetchone()
    if not group_row:
        bot.edit_message_text(
            "❌ Группа не найдена.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
        return
    group_name = group_row[0]

    # Получим задачи группы
    c.execute("""
        SELECT id, title, is_done, start_time, end_time
        FROM tasks
        WHERE group_id = ?
        ORDER BY is_done, start_time
    """, (group_id,))
    tasks = c.fetchall()

    if not tasks:
        bot.edit_message_text(
            f"📋 У группы <b>{group_name}</b> пока нет задач.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="HTML"
        )
        return

    text = f"<b>📋 Задачи группы:</b> <b>{group_name}</b>\n\n"
    markup = types.InlineKeyboardMarkup()

    for tid, title, is_done, start, end in tasks:
        status = "✅" if is_done else "⏳"

        # Преобразуем даты
        def format_time(t):
            try:
                dt = datetime.fromisoformat(t)
                return dt.strftime('%H:%M %d-%m-%Y')
            except:
                return "??"

        start_str = format_time(start)
        end_str = format_time(end) if end else ""

        button_text = f"{status} {title} ({start_str})"
        markup.add(types.InlineKeyboardButton(button_text, callback_data=f"/task_{tid}"))

    bot.edit_message_text(
        text=text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup,
        parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("/task_"))
def callback_task_details(call):
    task_id = int(call.data.split("_")[1])

    c.execute("""
        SELECT title, description, start_time, end_time, is_done
        FROM tasks
        WHERE id = ?
    """, (task_id,))
    task = c.fetchone()

    if not task:
        bot.answer_callback_query(call.id, "Задача не найдена.")
        return

    title, description, start, end, is_done = task
    status = "✅ Выполнена" if is_done else "⏳ В процессе"

    def fmt(t):
        try:
            return datetime.fromisoformat(t).strftime('%H:%M %d-%m-%Y')
        except:
            return "—"

    start_str = fmt(start)
    end_str = fmt(end) if end else ""

    text = (
        f"<b>📝 Задача:</b> {title}\n"
        f"<b>💬 Описание:</b> {description}\n"
        f"<b>🕒 Начало:</b> {start_str}\n"
        f"<b>⏱ Статус:</b> {status}"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✏️ Изменить", callback_data=f"edit_task_{task_id}"))
    markup.add(types.InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_task_{task_id}"))

    bot.edit_message_text(
        text=text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup,
        parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_task_"))
def callback_delete_task(call):
    task_id = int(call.data.split("_")[2])

    c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()

    bot.edit_message_text(
        text="🗑 Задача успешно удалена.",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_task_"))
def callback_edit_task(call):
    task_id = int(call.data.split("_")[2])
    telegram_id = call.from_user.id

    # Загружаем задачу
    c.execute("SELECT group_id, title, description FROM tasks WHERE id = ?", (task_id,))
    task = c.fetchone()
    if not task:
        bot.answer_callback_query(call.id, "Задача не найдена.")
        return

    group_id, title, description = task

    task_creation_state[telegram_id] = {
        "group_id": group_id,
        "task_id": task_id,
        "step": "title",
        "edit_mode": True
    }

    msg = bot.send_message(call.message.chat.id, "✏️ Введите новое название задачи:")
    bot.register_next_step_handler(msg, handle_task_creation_step)

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_group_"))
def callback_delete_group(call):
    group_id = int(call.data.split("_")[2])
    telegram_id = call.from_user.id

    # Получим ID пользователя
    c.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
    user_row = c.fetchone()
    if not user_row:
        bot.answer_callback_query(call.id, "Пользователь не найден.")
        return
    user_id = user_row[0]

    # Проверим, является ли пользователь владельцем группы
    c.execute("SELECT name FROM groups WHERE id = ? AND owner_id = ?", (group_id, user_id))
    group = c.fetchone()
    if not group:
        bot.answer_callback_query(call.id, "❌ Только владелец может удалить группу.")
        return

    group_name = group[0]

    # Удаляем задачи группы
    c.execute("DELETE FROM tasks WHERE group_id = ?", (group_id,))
    # Удаляем участников
    c.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
    # Удаляем саму группу
    c.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    conn.commit()

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🗑 Группа <b>{group_name}</b> и все связанные данные успешно удалены.",
        parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("group_members_"))
def callback_group_members(call):
    group_id = int(call.data.split("_")[2])
    telegram_id = call.from_user.id

    # Получаем ID пользователя
    c.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
    row = c.fetchone()
    if not row:
        bot.answer_callback_query(call.id, "Пользователь не найден.")
        return
    user_id = row[0]

    # Получаем имя группы и владельца
    c.execute("SELECT name, owner_id FROM groups WHERE id = ?", (group_id,))
    group = c.fetchone()
    if not group:
        bot.answer_callback_query(call.id, "Группа не найдена.")
        return
    group_name, owner_id = group

    # Получаем участников
    c.execute("""
        SELECT u.id, u.username, u.telegram_id
        FROM group_members gm
        JOIN users u ON u.id = gm.user_id
        WHERE gm.group_id = ?
        ORDER BY u.username
    """, (group_id,))
    members = c.fetchall()

    text = f"<b>👥 Участники группы:</b> <b>{group_name}</b>\n\n"

    markup = types.InlineKeyboardMarkup(row_width=1)

    for uid, username, tg_id in members:
        label = f"@{username}" if username else f"ID {tg_id}"
        prefix = "👑 " if uid == owner_id else "• "
        markup.add(types.InlineKeyboardButton(f"{prefix}{label}", callback_data="noop"))

    # Кнопка добавления участника
    markup.add(types.InlineKeyboardButton("➕ Добавить участника", callback_data=f"add_member_{group_id}"))

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        reply_markup=markup,
        parse_mode="HTML"
    )




if __name__ == "__main__":
    print("✅ Бот запущен. Ожидаю сообщения...")
    threading.Thread(target=notification_worker, daemon=True).start()
    bot.infinity_polling(timeout=60, long_polling_timeout = 10)
