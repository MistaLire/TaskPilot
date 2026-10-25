
import os
import sqlite3
import logging

from pathlib import Path
from datetime import datetime, timedelta
from contextlib import contextmanager

from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# Настройки
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = Path(__file__).with_name("tasks.db")
MAX_TASK_LENGTH = 200

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TASK_TEXT, TASK_DEADLINE, TASK_PRIORITY, TASK_CATEGORY = range(4)

# ---------- База данных ----------

@contextmanager
def get_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()



def init_db():
    with get_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS Tasks (
                TaskID INTEGER PRIMARY KEY AUTOINCREMENT,
                UserID INTEGER NOT NULL,
                TaskText TEXT NOT NULL,
                IsCompleted INTEGER NOT NULL DEFAULT 0,
                CreatedAt TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Получаем названия существующих столбцов
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(Tasks)"
            ).fetchall()
        }

        # Добавляем дедлайн, если его ещё нет
        if "Deadline" not in columns:
            connection.execute("""
                ALTER TABLE Tasks
                ADD COLUMN Deadline TEXT
            """)

        # Добавляем приоритет
        if "Priority" not in columns:
            connection.execute("""
                ALTER TABLE Tasks
                ADD COLUMN Priority TEXT NOT NULL
                DEFAULT 'Средний'
            """)

        # Добавляем категорию
        if "Category" not in columns:
            connection.execute("""
                ALTER TABLE Tasks
                ADD COLUMN Category TEXT NOT NULL
                DEFAULT 'Другое'
            """)



# ---------- Клавиатуры ----------

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["➕ Добавить задачу"],
            ["📋 Мои задачи"],
            ["ℹ️ Помощь"],
        ],
        resize_keyboard=True,
    )


def task_keyboard(task_id, is_completed):
    buttons = []

    if not is_completed:
        buttons.append(
            InlineKeyboardButton(
                "✅ Выполнить",
                callback_data=f"done:{task_id}",
            )
        )

    buttons.append(
        InlineKeyboardButton(
            "🗑 Удалить",
            callback_data=f"delete:{task_id}",
        )
    )

    return InlineKeyboardMarkup([buttons])


# ---------- Напоминания ----------

def reminder_job_name(task_id):
    return f"task_{task_id}"


def schedule_reminder(app, task):
    if not task["Deadline"] or task["IsCompleted"]:
        return

    try:
        deadline = datetime.fromisoformat(task["Deadline"])
    except ValueError:
        logger.warning("Некорректный дедлайн задачи %s", task["TaskID"])
        return

    now = datetime.now().astimezone()

    # Если дата без часового пояса, считаем её местным временем.
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=now.tzinfo)

    delay = (deadline - now).total_seconds()

    if delay <= 0:
        return

    job_queue = app.job_queue
    if job_queue is None:
        logger.error("JobQueue недоступен. Проверь установку зависимостей.")
        return

    job_queue.run_once(
        send_reminder,
        when=delay,
        data={
            "task_id": task["TaskID"],
            "user_id": task["UserID"],
            "task_text": task["TaskText"],
        },
        name=reminder_job_name(task["TaskID"]),
        chat_id=task["UserID"],
        user_id=task["UserID"],
    )


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data

    # Не отправляем напоминание, если задача удалена
    # или уже завершена.
    with get_connection() as connection:
        task = connection.execute(
            """SELECT TaskText, Deadline, IsCompleted
            FROM Tasks
            WHERE TaskID = ? AND UserID = ?""",
            (data["task_id"], data["user_id"]),
        ).fetchone()

    if not task or task["IsCompleted"] or not task["Deadline"]:
        return

    await context.bot.send_message(
        chat_id=data["user_id"],
        text=(
            "⏰ Наступил дедлайн!\n\n"
            f"Задача: {task['TaskText']}\n"
            f"Номер: {data['task_id']}"
        ),
    )


def restore_reminders(app):
    with get_connection() as connection:
        tasks = connection.execute(
            """SELECT TaskID, UserID, TaskText, Deadline, IsCompleted
            FROM Tasks
            WHERE Deadline IS NOT NULL AND IsCompleted = 0"""
        ).fetchall()

    for task in tasks:
        schedule_reminder(app, task)


# ---------- Команды и меню ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Привет! Я TaskPilot — твой помощник по задачам.\n\n"
        "Выбери действие в меню ниже.",
        reply_markup=main_keyboard(),
    )


async def help_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    await update.effective_message.reply_text(
        "Как пользоваться TaskPilot:\n\n"
        "➕ Добавить задачу — создать задачу и при желании задать дедлайн.\n"
        "📋 Мои задачи — показать список и кнопки действий.\n\n"
        "Команды:\n"
        "/start — главное меню\n"
        "/add текст — добавить задачу без дедлайна\n"
        "/list — показать задачи\n"
        "/done номер — завершить задачу\n"
        "/delete номер — удалить задачу\n\n"
        "Максимальная длина текста задачи — 200 символов.",
        reply_markup=main_keyboard(),
    )


# ---------- Добавление задачи ----------

async def add_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    await update.effective_message.reply_text(
        "Напиши текст задачи (не более 200 символов).\n\n"
        "Для отмены отправь /cancel."
    )
    return TASK_TEXT


async def add_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    text = (update.effective_message.text or "").strip()

    if not text or len(text) > MAX_TASK_LENGTH:
        await update.effective_message.reply_text(
            "Текст должен содержать от 1 до 200 символов. "
            "Попробуй ещё раз."
        )
        return TASK_TEXT

    context.user_data["new_task_text"] = text

    await update.effective_message.reply_text(
        "Укажи дедлайн в формате:\n"
        "2026-12-31 18:30\n\n"
        "Или отправь 0, чтобы создать задачу без дедлайна."
    )
    return TASK_DEADLINE



async def add_deadline(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    value = (update.effective_message.text or "").strip()
    deadline_text = None

    if value != "0":
        try:
            deadline = datetime.strptime(value, "%Y-%m-%d %H:%M")
        except ValueError:
            await update.effective_message.reply_text(
                "Неверный формат даты.\n"
                "Пример: 2026-12-31 18:30\n"
                "Или отправь 0 без дедлайна."
            )
            return TASK_DEADLINE

        if deadline <= datetime.now():
            await update.effective_message.reply_text(
                "Дедлайн должен быть в будущем. Укажи другую дату."
            )
            return TASK_DEADLINE

        deadline_text = deadline.replace(
            tzinfo=datetime.now().astimezone().tzinfo
        ).isoformat()

    context.user_data["new_task_deadline"] = deadline_text

    keyboard = ReplyKeyboardMarkup(
        [["Высокий", "Средний", "Низкий"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await update.effective_message.reply_text(
        "Выбери приоритет задачи:",
        reply_markup=keyboard,
    )
    return TASK_PRIORITY


async def add_priority(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    priority = (update.effective_message.text or "").strip()
    priorities = ["Высокий", "Средний", "Низкий"]

    if priority not in priorities:
        await update.effective_message.reply_text(
            "Выбери один из вариантов: Высокий, Средний или Низкий."
        )
        return TASK_PRIORITY

    context.user_data["new_task_priority"] = priority

    keyboard = ReplyKeyboardMarkup(
        [
            ["Учёба", "Работа"],
            ["Личное", "Другое"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await update.effective_message.reply_text(
        "Выбери категорию задачи:",
        reply_markup=keyboard,
    )
    return TASK_CATEGORY


async def add_category(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    category = (update.effective_message.text or "").strip()
    categories = ["Учёба", "Работа", "Личное", "Другое"]

    if category not in categories:
        await update.effective_message.reply_text(
            "Выбери категорию: Учёба, Работа, Личное или Другое."
        )
        return TASK_CATEGORY

    task_text = context.user_data.pop("new_task_text")
    deadline_text = context.user_data.pop("new_task_deadline")
    priority = context.user_data.pop("new_task_priority")
    user_id = update.effective_user.id

    try:
        with get_connection() as connection:
            cursor = connection.execute(
                """INSERT INTO Tasks
                (UserID, TaskText, Deadline, Priority, Category)
                VALUES (?, ?, ?, ?, ?)""",
                (user_id, task_text, deadline_text, priority, category),
            )
            task_id = cursor.lastrowid

            task = connection.execute(
                """SELECT TaskID, UserID, TaskText,
                Deadline, IsCompleted
                FROM Tasks WHERE TaskID = ?""",
                (task_id,),
            ).fetchone()

        schedule_reminder(context.application, task)

    except sqlite3.Error:
        logger.exception("Ошибка при добавлении задачи")
        await update.effective_message.reply_text(
            "Не удалось сохранить задачу. Попробуй ещё раз.",
            reply_markup=main_keyboard(),
        )
        return ConversationHandler.END

    reply = (
        f"Задача №{task_id} добавлена!\n"
        f"Текст: {task_text}\n"
        f"Приоритет: {priority}\n"
        f"Категория: {category}"
    )

    if deadline_text:
        deadline = datetime.fromisoformat(deadline_text)
        reply += "\nДедлайн: " + deadline.strftime("%d.%m.%Y %H:%M")
    else:
        reply += "\nБез дедлайна"

    await update.effective_message.reply_text(
        reply,
        reply_markup=main_keyboard(),
    )
    return ConversationHandler.END



async def cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    context.user_data.pop("new_task_text", None)
    await update.effective_message.reply_text(
        "Добавление отменено.",
        reply_markup=main_keyboard(),
    )
    return ConversationHandler.END


async def add_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    text = " ".join(context.args).strip()

    if not text:
        await update.effective_message.reply_text(
            "Пример: /add Купить молоко"
        )
        return

    if len(text) > MAX_TASK_LENGTH:
        await update.effective_message.reply_text(
            "Текст задачи не должен превышать 200 символов."
        )
        return

    try:
        with get_connection() as connection:
            cursor = connection.execute(
                "INSERT INTO Tasks (UserID, TaskText) VALUES (?, ?)",
                (update.effective_user.id, text),
            )
            task_id = cursor.lastrowid

    except sqlite3.Error:
        logger.exception("Ошибка при добавлении задачи")
        await update.effective_message.reply_text(
            "Не удалось сохранить задачу."
        )
        return

    await update.effective_message.reply_text(
        f"Задача №{task_id} добавлена.",
        reply_markup=main_keyboard(),
    )


# ---------- Список задач ----------

async def list_tasks(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id

    with get_connection() as connection:
        tasks = connection.execute(
            """SELECT TaskID, TaskText, IsCompleted, Deadline,
            Priority, Category
            FROM Tasks WHERE UserID = ?
            ORDER BY IsCompleted, TaskID""",    
            (user_id,),
        ).fetchall()

    if not tasks:
        await update.effective_message.reply_text(
            "Список пуст. Нажми «➕ Добавить задачу».",
            reply_markup=main_keyboard(),
        )
        return

    for task in tasks:
        status = "Выполнена" if task["IsCompleted"] else "В работе"
        message = (
            f"Задача №{task['TaskID']}\n"
            f"{task['TaskText']}\n"
            f"Статус: {status}\n"
            f"Приоритет: {task['Priority']}\n"
            f"Категория: {task['Category']}"
        )

        if task["Deadline"]:
            try:
                deadline = datetime.fromisoformat(task["Deadline"])
                message += (
                    "\nДедлайн: "
                    + deadline.strftime("%d.%m.%Y %H:%M")
                )
            except ValueError:
                message += "\nДедлайн: ошибка формата даты"

        await update.effective_message.reply_text(
            message,
            reply_markup=task_keyboard(
                task["TaskID"], task["IsCompleted"]
            ),
        )


# ---------- Завершение и удаление ----------

async def task_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    try:
        action, raw_id = query.data.split(":")
        task_id = int(raw_id)
    except (ValueError, AttributeError):
        await query.message.reply_text("Некорректное действие.")
        return

    user_id = query.from_user.id

    try:
        with get_connection() as connection:
            if action == "done":
                cursor = connection.execute(
                    """UPDATE Tasks SET IsCompleted = 1
                    WHERE TaskID = ? AND UserID = ?""",
                    (task_id, user_id),
                )
            elif action == "delete":
                cursor = connection.execute(
                    """DELETE FROM Tasks
                    WHERE TaskID = ? AND UserID = ?""",
                    (task_id, user_id),
                )
            else:
                await query.message.reply_text(
                    "Неизвестное действие."
                )
                return

            changed = cursor.rowcount

    except sqlite3.Error:
        logger.exception("Ошибка при изменении задачи")
        await query.message.reply_text(
            "Не удалось изменить задачу."
        )
        return

    if changed == 0:
        await query.message.reply_text(
            "Задача не найдена или уже удалена."
        )
        return

    job = context.job_queue.get_jobs_by_name(
        reminder_job_name(task_id)
    ) if context.job_queue else []

    for scheduled_job in job:
        scheduled_job.schedule_removal()

    if action == "done":
        await query.edit_message_text(
            f"Задача №{task_id} выполнена."
        )
    else:
        await query.edit_message_text(
            f"Задача №{task_id} удалена."
        )


async def done_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    await simple_task_command(update, context, "done")


async def delete_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    await simple_task_command(update, context, "delete")


async def simple_task_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
):
    if not context.args:
        await update.effective_message.reply_text(
            f"Укажи номер задачи. Пример: /{action} 1"
        )
        return

    try:
        task_id = int(context.args[0])
        if task_id < 1:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            "Номер задачи должен быть положительным числом."
        )
        return

    try:
        with get_connection() as connection:
            if action == "done":
                cursor = connection.execute(
                    """UPDATE Tasks SET IsCompleted = 1
                    WHERE TaskID = ? AND UserID = ?""",
                    (task_id, update.effective_user.id),
                )
            else:
                cursor = connection.execute(
                    """DELETE FROM Tasks
                    WHERE TaskID = ? AND UserID = ?""",
                    (task_id, update.effective_user.id),
                )
            changed = cursor.rowcount

    except sqlite3.Error:
        logger.exception("Ошибка выполнения команды")
        await update.effective_message.reply_text(
            "Ошибка базы данных. Попробуй ещё раз."
        )
        return

    if not changed:
        await update.effective_message.reply_text(
            "Задача не найдена в твоём списке."
        )
        return

    if context.job_queue:
        for job in context.job_queue.get_jobs_by_name(
            reminder_job_name(task_id)
        ):
            job.schedule_removal()

    result = "выполнена" if action == "done" else "удалена"
    await update.effective_message.reply_text(
        f"Задача №{task_id} {result}."
    )


# ---------- Запуск ----------

async def post_init(app: Application):
    restore_reminders(app)


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
):
    logger.error(
        "Необработанная ошибка при обработке обновления",
        exc_info=context.error,
    )


def main():
    if not TOKEN:
        raise ValueError(
            "Не найден BOT_TOKEN в файле .env"
        )

    init_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    add_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("newtask", add_start),
            MessageHandler(
                filters.Regex("^➕ Добавить задачу$"),
                add_start,
            ),
        ],
                states={
            TASK_TEXT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    add_text,
                )
            ],
            TASK_DEADLINE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    add_deadline,
                )
            ],
            TASK_PRIORITY: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    add_priority,
                )
            ],
            TASK_CATEGORY: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    add_category,
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("list", list_tasks))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("delete", delete_command))
    app.add_handler(add_conversation)
    app.add_handler(
        MessageHandler(
            filters.Regex("^📋 Мои задачи$"),
            list_tasks,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.Regex("^ℹ️ Помощь$"),
            help_command,
        )
    )
    app.add_handler(CallbackQueryHandler(task_action))
    app.add_error_handler(error_handler)

    print("TaskPilot запущен!")
    print(f"База данных: {DB_PATH}")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
