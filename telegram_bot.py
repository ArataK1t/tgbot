from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, JobQueue
from subprocess import check_output, CalledProcessError
import docker
import psutil
from datetime import datetime, timezone, timedelta

# Инициализация Docker клиента
client = docker.from_env()

# Глобальные переменные для отслеживания состояния
active_jobs = {}
notification_history = []
notified_containers = set()
container_states = {}

# Функция для получения системных метрик
def get_system_metrics():
    cpu_usage = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage('/')
    return (f"CPU Usage: {cpu_usage}%\n"
            f"Memory Usage: {memory.percent}% ({memory.used / 1024 / 1024 / 1024:.2f} GB / {memory.total / 1024 / 1024 / 1024:.2f} GB)\n"
            f"Swap Usage: {swap.percent}% ({swap.used / 1024 / 1024 / 1024:.2f} GB / {swap.total / 1024 / 1024 / 1024:.2f} GB)\n"
            f"Disk Usage: {disk.percent}% ({disk.used / 1024 / 1024 / 1024:.2f} GB / {disk.total / 1024 / 1024 / 1024:.2f} GB)")

# Функция для получения статуса контейнеров
def get_container_status():
    containers = client.containers.list(all=True)
    status = "\n".join([f"{c.name}: {c.status}" for c in containers])
    return status

# Функция для проверки статусов контейнеров и отправки уведомлений
def check_container_health_and_notify(context):
    global container_states

    # Получаем список всех контейнеров
    containers = client.containers.list(all=True)

    for container in containers:
        container_name = container.name
        current_status = container.status

        # Если контейнер новый или его состояние изменилось
        if container_name not in container_states or container_states[container_name] != current_status:
            container_states[container_name] = current_status  # Обновляем состояние

            if current_status in ['exited', 'stopped', 'unhealthy']:
                # Отправляем уведомление только для проблемных состояний
                message = f"❗ Контейнер {container_name} в состоянии {current_status}."
                add_notification_to_history(message)
                context.bot.send_message(
                    chat_id=context.job.context['chat_id'],
                    text=message,
                    disable_notification=False
                )

        # Если контейнер вернулся в нормальное состояние (например, running), обновляем состояние
        elif current_status == 'running' and container_name in notified_containers:
            notified_containers.remove(container_name)

def clear_notification_history(update: Update, context):
    global notification_history, notification_messages
    query = update.callback_query
    query.answer()

    # Очищаем историю уведомлений
    notification_history = []

    # Удаляем все сообщения с уведомлениями
    for message_id in notification_messages:
        try:
            context.bot.delete_message(chat_id=query.message.chat_id, message_id=message_id)
        except Exception as e:
            # Логирование ошибки на русском языке
            query.edit_message_text(f"Ошибка при удалении сообщений: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2b05 Назад", callback_data='back_to_menu')]]))

    # Очищаем список сообщений
    notification_messages.clear()

    # Возвращаем обновленный текст истории
    query.edit_message_text(
        "История уведомлений очищена.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2b05 Назад", callback_data='back_to_menu')]])
    )

# Функция для получения последних строк из screen-сессий
def get_screen_logs(session_name, lines=20):
    try:
        check_output(["screen", "-S", session_name, "-X", "hardcopy", "/tmp/screenlog.txt"])
        with open("/tmp/screenlog.txt", "r") as log_file:
            logs = log_file.readlines()[-lines:]
        return f"Logs for session {session_name}:\n" + "".join(logs)
    except Exception as e:
        return f"Error: {e}"

# Функция для добавления уведомления в историю
def add_notification_to_history(message):
    now = datetime.now(timezone.utc).astimezone(tz=timezone(timedelta(hours=3)))
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    notification_history.append(f"[{timestamp}] {message}")
    if len(notification_history) > 50:
        notification_history.pop(0)

# Главное меню
def start(update: Update, context):
    keyboard = [
        [InlineKeyboardButton("\ud83d\udcca Метрики", callback_data='metrics')],
        [InlineKeyboardButton("\ud83d\udce6 Статус контейнеров", callback_data='container_status')],
        [InlineKeyboardButton("\ud83d\udd0d Логи контейнера", callback_data='container_logs')],
        [InlineKeyboardButton("\ud83d\udd0e История уведомлений", callback_data='notification_history')],
        [InlineKeyboardButton("\u2753 Помощь", callback_data='help')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text("Главное меню", reply_markup=reply_markup)

# Функция для обновления сообщений в реальном времени
def update_message(context):
    job_data = context.job.context
    new_text = job_data['callback']()
    context.bot.edit_message_text(chat_id=job_data['chat_id'],
                                  message_id=job_data['message_id'],
                                  text=new_text,
                                  reply_markup=job_data.get('reply_markup'))

# Универсальная кнопка "Назад" для возврата в меню
def back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("\u2b05 Назад", callback_data='back_to_menu')]])

# Режим обновления метрик
def show_metrics(update: Update, context):
    query = update.callback_query
    query.answer()
    
    # Остановка старой задачи
    stop_job_for_chat(query.message.chat_id)

    job_context = {
        'chat_id': query.message.chat_id,
        'message_id': query.message.message_id,
        'callback': get_system_metrics,
        'reply_markup': back_button()
    }
    job = context.job_queue.run_repeating(update_message, interval=5, context=job_context)
    active_jobs[query.message.chat_id] = job
    query.edit_message_text(get_system_metrics(), reply_markup=back_button())

# Режим обновления статуса контейнеров
def show_container_status(update: Update, context):
    query = update.callback_query
    query.answer()

    # Остановка старой задачи
    stop_job_for_chat(query.message.chat_id)

    job_context = {
        'chat_id': query.message.chat_id,
        'message_id': query.message.message_id,
        'callback': get_container_status,
        'reply_markup': back_button()
    }
    job = context.job_queue.run_repeating(update_message, interval=10, context=job_context)
    active_jobs[query.message.chat_id] = job
    query.edit_message_text(get_container_status(), reply_markup=back_button())

# Выбор screen-сессии для логов
def select_screen_session(update: Update, context):
    query = update.callback_query
    query.answer()

    try:
        sessions = check_output(["screen", "-ls"]).decode('utf-8')
        session_names = [line.split()[0] for line in sessions.splitlines() if "Detached" in line or "Attached" in line]
        keyboard = [[InlineKeyboardButton(name, callback_data=f'screen_logs_{name}')]
                    for name in session_names]
    except CalledProcessError:
        keyboard = []

    keyboard.append([InlineKeyboardButton("\u2b05 Назад", callback_data='back_to_menu')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    query.edit_message_text("Выберите screen-сессию для просмотра логов:", reply_markup=reply_markup)

# Режим обновления логов screen-сессии
def show_screen_logs(update: Update, context):
    query = update.callback_query
    session_name = query.data.split('_', 2)[2]
    query.answer()

    # Остановка старой задачи
    stop_job_for_chat(query.message.chat_id)

    def logs_callback():
        return get_screen_logs(session_name)

    job_context = {
        'chat_id': query.message.chat_id,
        'message_id': query.message.message_id,
        'callback': logs_callback,
        'reply_markup': back_button()
    }
    job = context.job_queue.run_repeating(update_message, interval=10, context=job_context)
    active_jobs[query.message.chat_id] = job
    query.edit_message_text(get_screen_logs(session_name), reply_markup=back_button())

# История уведомлений
def show_notification_history(update: Update, context):
    query = update.callback_query
    query.answer()

    history_text = "\n".join(notification_history) if notification_history else "История пуста."
    keyboard = [
        [InlineKeyboardButton("\ud83d\uddd1\ufe0f Очистить историю", callback_data='clear_notification_history')],
        [InlineKeyboardButton("\u2b05 Назад", callback_data='back_to_menu')]
    ]
    query.edit_message_text(f"История уведомлений:\n{history_text}", reply_markup=InlineKeyboardMarkup(keyboard))

# Остановка задач для чата
def stop_job_for_chat(chat_id):
    if chat_id in active_jobs:
        active_jobs[chat_id].schedule_removal()
        del active_jobs[chat_id]

# Удаление сообщения и возврат в меню
def back_to_menu(update: Update, context):
    query = update.callback_query
    query.answer()

    # Остановка старой задачи
    stop_job_for_chat(query.message.chat_id)

    # Удаляем текущее сообщение
    context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)

    # Отправляем главное меню
    start(query, context)

# Обработчик кнопок
def button(update: Update, context):
    query = update.callback_query
    if query.data == 'metrics':
        show_metrics(update, context)
    elif query.data == 'container_status':
        show_container_status(update, context)
    elif query.data == 'container_logs':
        select_screen_session(update, context)
    elif query.data.startswith('screen_logs_'):
        show_screen_logs(update, context)
    elif query.data == 'notification_history':
        show_notification_history(update, context)
    elif query.data == 'clear_notification_history':
        clear_notification_history(update, context)    
    elif query.data == 'back_to_menu':
        back_to_menu(update, context)
    elif query.data == 'help':
        query.edit_message_text(
            "Помощь:\n1. Метрики - показывает загрузку CPU и памяти.\n2. Статус контейнеров - текущий статус ваших контейнеров.\n3. Логи контейнера - выберите screen-сессию для просмотра последних строк логов.\n4. История уведомлений - последние уведомления о состоянии контейнеров.",
            reply_markup=back_button()
        )

# Запуск бота
def main():
    global container_states

    containers = client.containers.list(all=True)
    container_states = {container.name: container.status for container in containers}
    
    updater = Updater("your_telegram_bot_token", use_context=True)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler('start', start))
    dispatcher.add_handler(CallbackQueryHandler(button))

    job_queue = updater.job_queue
    job_queue.run_repeating(check_container_health_and_notify, interval=60, first=0, context={'chat_id': 'your_chat_id'})

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
