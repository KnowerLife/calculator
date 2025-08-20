import logging
import requests
import cv2
import numpy as np
from pyzbar.pyzbar import decode
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, ContextTypes, filters
from telegram.error import BadRequest
import re
import tempfile
import os
import csv
import io
import matplotlib.pyplot as plt

# Настройки
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния диалога
(
    SELECTING_ACTION, ADDING_MEMBERS, SELECTING_PAYER, 
    ADDING_PRODUCT_NAME, ADDING_PRODUCT_PRICE, SELECTING_PRODUCT_TYPE, 
    SELECTING_PRODUCT_PARTICIPANTS, PROCESSING_QR, PROCESSING_CSV, CONFIRMING_ASSIGNMENTS
) = range(10)

# API для чека и платежей
FNS_API_URL = "https://proverkacheka.com/api/v1/check/get"
FNS_API_KEY = os.getenv("FNS_API_KEY", "TOKEN")
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "your_payment_provider_token")  # Замените на реальный токен

# Глобальное хранилище
user_data = {}

class Receipt:
    def __init__(self):
        self.payer = None
        self.items = []
        self.shared_items = []
    
    def add_item(self, name, price, quantity=1, members=None):
        if members:
            self.items.append({"name": name, "price": price, "quantity": quantity, "members": members})
        else:
            self.shared_items.append({"name": name, "price": price, "quantity": quantity})
    
    def calculate(self, members):
        calculations = {member: 0 for member in members}
        
        # Общие товары
        for item in self.shared_items:
            share = (item['price'] * item['quantity']) / len(members)
            for member in members:
                calculations[member] += share
        
        # Индивидуальные товары
        for item in self.items:
            share = (item['price'] * item['quantity']) / len(item['members'])
            for member in item['members']:
                calculations[member] += share
        
        # Формируем итог
        result = []
        total = sum(item['price'] * item['quantity'] for item in self.shared_items + self.items)
        result.append(f"Общая сумма: {total:.2f}₽")
        result.append(f"Оплатил(а): {self.payer}")
        
        debts = []
        for member, amount in calculations.items():
            if member != self.payer and amount > 0:
                debts.append((member, amount))
                result.append(f"{member} должен {amount:.2f}₽ {self.payer}")
        
        return "\n".join(result), debts
    
    def generate_verification_list(self, members):
        """Генерирует список для сверки: кто за что платит"""
        def truncate_name(name, max_length=50):
            """Обрезает длинные названия продуктов"""
            return name[:max_length] + "..." if len(name) > max_length else name
        
        result = ["\n--- Список для сверки ---"]
        
        # Общие товары
        if self.shared_items:
            result.append("Общие товары (делятся на всех):")
            for item in self.shared_items:
                result.append(
                    f"- {truncate_name(item['name'])}: {item['price']:.2f}₽ x {item['quantity']} "
                    f"(все участники: {', '.join(members)})"
                )
        
        # Индивидуальные товары
        if self.items:
            result.append("Индивидуальные товары:")
            for item in self.items:
                result.append(
                    f"- {truncate_name(item['name'])}: {item['price']:.2f}₽ x {item['quantity']} "
                    f"(участники: {', '.join(item['members'])})"
                )
        
        if not self.shared_items and not self.items:
            result.append("Нет товаров для сверки.")
        
        return "\n".join(result)
    
    def generate_verification_csv(self, members):
        """Генерирует CSV с детализацией товаров, участников и итогами"""
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';', lineterminator='\n')
        writer.writerow(['Тип', 'Товар', 'Цена', 'Количество', 'Участники'])
        
        # Общие товары
        for item in self.shared_items:
            writer.writerow([
                'Общий',
                item['name'],
                f"{item['price']:.2f}",
                item['quantity'],
                ', '.join(members)
            ])
        
        # Индивидуальные товары
        for item in self.items:
            writer.writerow([
                'Индивидуальный',
                item['name'],
                f"{item['price']:.2f}",
                item['quantity'],
                ', '.join(item['members'])
            ])
        
        # Добавляем итоги
        writer.writerow([])  # Пустая строка для разделения
        calculations = {member: 0 for member in members}
        for item in self.shared_items:
            share = (item['price'] * item['quantity']) / len(members)
            for member in members:
                calculations[member] += share
        for item in self.items:
            share = (item['price'] * item['quantity']) / len(item['members'])
            for member in item['members']:
                calculations[member] += share
        total = sum(item['price'] * item['quantity'] for item in self.shared_items + self.items)
        
        writer.writerow([f"Общая сумма: {total:.2f}₽"])
        writer.writerow([f"Оплатил(а): {self.payer}"])
        for member, amount in calculations.items():
            if member != self.payer:
                writer.writerow([f"{member} должен {amount:.2f}₽ {self.payer}"])
        
        csv_content = output.getvalue()
        output.close()
        return csv_content

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data[user_id] = {
        "members": [],
        "receipt": Receipt(),
        "current_product": {},
        "csv_products": [],
        "product_assignments": {},
        "current_product_index": 0
    }
    
    keyboard = [["Добавить участников", "Начать расчет"]]
    await update.message.reply_text(
        "Добро пожаловать в бот для расчета общих покупок!\n\n"
        "Сначала добавьте участников, затем начните расчет.",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
    )
    return SELECTING_ACTION

async def select_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    
    if text == "Добавить участников":
        await update.message.reply_text(
            "Введите имена участников через запятую:\n"
            "Например: Алексей, Мария, Иван, Елена",
            reply_markup=ReplyKeyboardRemove()
        )
        return ADDING_MEMBERS
    elif text == "Начать расчет":
        if not user_data[user_id]["members"]:
            await update.message.reply_text(
                "Сначала нужно добавить участников!",
                reply_markup=ReplyKeyboardMarkup([["Добавить участников"]], one_time_keyboard=True)
            )
            return SELECTING_ACTION
        
        keyboard = [[member] for member in user_data[user_id]["members"]]
        await update.message.reply_text(
            "Кто оплатил покупки?",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
        )
        return SELECTING_PAYER
    return SELECTING_ACTION

async def add_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    
    members = [name.strip() for name in text.split(",") if name.strip()]
    if not members:
        await update.message.reply_text(
            "Неверный формат. Введите имена через запятую:",
            reply_markup=ReplyKeyboardRemove()
        )
        return ADDING_MEMBERS
    
    user_data[user_id]["members"] = members
    keyboard = [["Добавить участников", "Начать расчет"]]
    await update.message.reply_text(
        f"Участники добавлены: {', '.join(members)}\n\n"
        "Что делаем дальше?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
    )
    return SELECTING_ACTION

async def select_payer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payer = update.message.text
    
    if payer not in user_data[user_id]["members"]:
        await update.message.reply_text(
            "Выберите участника из списка:",
            reply_markup=ReplyKeyboardMarkup([[member] for member in user_data[user_id]["members"]], one_time_keyboard=True)
        )
        return SELECTING_PAYER
    
    user_data[user_id]["receipt"].payer = payer
    keyboard = [["Добавить продукт", "Сканировать QR-код", "Загрузить CSV"], ["Завершить расчет"]]
    await update.message.reply_text(
        f"Оплатил(а): {payer}\n\n"
        "Теперь добавляйте продукты:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
    )
    return ADDING_PRODUCT_NAME

async def add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    
    if text == "Добавить продукт":
        await update.message.reply_text(
            "Введите название продукта:",
            reply_markup=ReplyKeyboardRemove()
        )
        return ADDING_PRODUCT_NAME
    elif text == "Сканировать QR-код":
        await update.message.reply_text(
            "Отправьте фото QR-кода или введите данные вручную в формате:\n"
            "t=20230101T1200&s=1000.00&fn=1234567890&i=12345&fp=1234567890",
            reply_markup=ReplyKeyboardRemove()
        )
        return PROCESSING_QR
    elif text == "Загрузить CSV":
        await update.message.reply_text(
            "Отправьте CSV файл с колонками: Товар,Цена,Количество (Количество необязательно).\n"
            "Пример:\nТовар;Цена;Количество\nХлеб;100,50;2\nМолоко;60,75;1",
            reply_markup=ReplyKeyboardRemove()
        )
        return PROCESSING_CSV
    elif text == "Завершить расчет":
        return await show_product_list(update, context)
    else:
        user_data[user_id]["current_product"] = {"name": text}
        await update.message.reply_text(
            "Теперь введите цену продукта:",
            reply_markup=ReplyKeyboardRemove()
        )
        return ADDING_PRODUCT_PRICE

async def add_product_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    
    try:
        price = float(text.replace(',', '.'))
        user_data[user_id]["current_product"]["price"] = price
        await update.message.reply_text(
            "Выберите тип товара:",
            reply_markup=ReplyKeyboardMarkup([["Общий", "Индивидуальный"]], one_time_keyboard=True)
        )
        return SELECTING_PRODUCT_TYPE
    except ValueError:
        await update.message.reply_text(
            "Неверный формат цены. Введите число:",
            reply_markup=ReplyKeyboardRemove()
        )
        return ADDING_PRODUCT_PRICE

async def select_product_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    
    if text not in ["Общий", "Индивидуальный"]:
        await update.message.reply_text(
            "Выберите тип товара:",
            reply_markup=ReplyKeyboardMarkup([["Общий", "Индивидуальный"]], one_time_keyboard=True)
        )
        return SELECTING_PRODUCT_TYPE
    
    # Сохраняем тип товара
    user_data[user_id]["current_product"]["type"] = "shared" if text == "Общий" else "individual"
    user_data[user_id]["csv_products"].append(user_data[user_id]["current_product"])
    
    if text == "Общий":
        # Для общих товаров автоматически назначаем всех участников
        product_index = len(user_data[user_id]["csv_products"]) - 1
        user_data[user_id]["product_assignments"][product_index] = user_data[user_id]["members"].copy()
        keyboard = [["Добавить продукт", "Сканировать QR-код", "Загрузить CSV"], ["Завершить расчет"]]
        await update.message.reply_text(
            f"Добавлен общий продукт: {user_data[user_id]['current_product']['name']} - {user_data[user_id]['current_product']['price']:.2f}₽",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
        )
        return ADDING_PRODUCT_NAME
    else:
        # Для индивидуальных товаров переходим к выбору участников
        user_data[user_id]["current_product_index"] = len(user_data[user_id]["csv_products"]) - 1
        return await show_product_list(update, context)

async def show_product_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not user_data[user_id]["csv_products"] and not user_data[user_id]["receipt"].items and not user_data[user_id]["receipt"].shared_items:
        await update.message.reply_text(
            "Не добавлено ни одного продукта!",
            reply_markup=ReplyKeyboardMarkup([["Добавить продукт", "Сканировать QR-код", "Загрузить CSV"], ["Завершить расчет"]], one_time_keyboard=True)
        )
        return ADDING_PRODUCT_NAME
    
    if update.message:
        user_data[user_id]["current_product_index"] = 0
    
    current_index = user_data[user_id]["current_product_index"]
    if current_index >= len(user_data[user_id]["csv_products"]):
        await (update.message or update.callback_query.message).reply_text(
            "Все продукты распределены. Нажмите 'Готово' для завершения.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Готово", callback_data="done_assignments")]])
        )
        return CONFIRMING_ASSIGNMENTS
    
    product = user_data[user_id]["csv_products"][current_index]
    product_type = "Общий" if product.get("type") == "shared" else "Индивидуальный"
    message_parts = [
        f"Продукт {current_index + 1} из {len(user_data[user_id]['csv_products'])} ({product_type}):",
        f"{product['name']} - {product['price']:.2f}₽ x {product.get('quantity', 1)}"
    ]
    assigned = user_data[user_id]["product_assignments"].get(current_index, user_data[user_id]["members"] if product.get("type") == "shared" else [])
    if assigned == user_data[user_id]["members"]:
        message_parts.append(f"(все участники: {', '.join(assigned)})")
    elif assigned:
        message_parts.append(f"(участники: {', '.join(assigned)})")
    else:
        message_parts.append("(участники: не выбраны)")
    
    # Кнопки для участников (только для индивидуальных товаров)
    buttons = []
    if product.get("type") == "individual":
        buttons = [
            InlineKeyboardButton(
                f"{member} ✓" if member in assigned else member,
                callback_data=f"assign_{current_index}_{member}"
            )
            for member in user_data[user_id]["members"]
        ]
        buttons.append(InlineKeyboardButton(
            "Все ✓" if assigned == user_data[user_id]["members"] else "Все",
            callback_data=f"assign_{current_index}_shared"
        ))
    
    # Кнопка изменения типа
    type_button = [InlineKeyboardButton(
        "Сделать индивидуальным" if product.get("type") == "shared" else "Сделать общим",
        callback_data=f"change_type_{current_index}"
    )]
    
    # Навигационные кнопки
    nav_buttons = []
    if current_index > 0:
        nav_buttons.append(InlineKeyboardButton("Назад", callback_data="prev_product"))
    if current_index < len(user_data[user_id]["csv_products"]) - 1:
        nav_buttons.append(InlineKeyboardButton("Далее", callback_data="next_product"))
    else:
        nav_buttons.append(InlineKeyboardButton("Готово", callback_data="done_assignments"))
    
    keyboard = [buttons, type_button, nav_buttons] if buttons else [type_button, nav_buttons]
    
    try:
        if update.message:
            await update.message.reply_text(
                "\n".join(message_parts),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.callback_query.message.edit_text(
                "\n".join(message_parts),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    except BadRequest as e:
        logger.error(f"Error updating message: {e}")
        await (update.message or update.callback_query.message).reply_text(
            "Ошибка при отображении продукта. Попробуйте снова."
        )
        return ADDING_PRODUCT_NAME
    
    logger.info(f"Navigating to product index {current_index}")
    return CONFIRMING_ASSIGNMENTS

async def handle_assignment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if data == "done_assignments":
        unassigned = [
            i for i, product in enumerate(user_data[user_id]["csv_products"])
            if product.get("type") == "individual" and 
            (i not in user_data[user_id]["product_assignments"] or not user_data[user_id]["product_assignments"][i])
        ]
        if unassigned:
            await query.message.reply_text(
                f"Не все индивидуальные продукты распределены! Выберите участников для продуктов: {', '.join(str(i + 1) for i in unassigned)}"
            )
            return CONFIRMING_ASSIGNMENTS
        
        logger.info(f"Product assignments: {user_data[user_id]['product_assignments']}")
        receipt = user_data[user_id]["receipt"]
        for i, product in enumerate(user_data[user_id]["csv_products"]):
            members = user_data[user_id]["product_assignments"].get(i, user_data[user_id]["members"] if product.get("type") == "shared" else [])
            receipt.add_item(
                product["name"],
                product["price"],
                product.get("quantity", 1),
                members if members != user_data[user_id]["members"] else None
            )
        
        return await calculate(update, context)
    
    if data == "next_product":
        current_index = user_data[user_id]["current_product_index"]
        if user_data[user_id]["csv_products"][current_index].get("type") == "individual" and \
           (current_index not in user_data[user_id]["product_assignments"] or not user_data[user_id]["product_assignments"][current_index]):
            await query.message.reply_text(
                "Выберите участников для текущего индивидуального продукта перед переходом к следующему!"
            )
            return CONFIRMING_ASSIGNMENTS
        
        user_data[user_id]["current_product_index"] += 1
        logger.info(f"Navigating to product index {user_data[user_id]['current_product_index']}")
        return await show_product_list(update, context)
    
    if data == "prev_product":
        if user_data[user_id]["current_product_index"] > 0:
            user_data[user_id]["current_product_index"] -= 1
            logger.info(f"Navigating to product index {user_data[user_id]['current_product_index']}")
            return await show_product_list(update, context)
        else:
            await query.message.reply_text("Это первый продукт, назад нельзя!")
            return CONFIRMING_ASSIGNMENTS
    
    # Обработка изменения типа товара
    match = re.match(r"change_type_(\d+)", data)
    if match:
        product_index = int(match.group(1))
        if product_index != user_data[user_id]["current_product_index"]:
            await query.message.reply_text("Ошибка: продукт не соответствует текущему.")
            return CONFIRMING_ASSIGNMENTS
        
        current_type = user_data[user_id]["csv_products"][product_index].get("type")
        new_type = "individual" if current_type == "shared" else "shared"
        user_data[user_id]["csv_products"][product_index]["type"] = new_type
        if new_type == "shared":
            user_data[user_id]["product_assignments"][product_index] = user_data[user_id]["members"].copy()
        else:
            user_data[user_id]["product_assignments"][product_index] = []
        logger.info(f"Changed product {product_index} to type {new_type}")
        return await show_product_list(update, context)
    
    # Обработка оплаты долга
    match = re.match(r"pay_(.+)_(\d+\.\d+)", data)
    if match:
        member, amount = match.group(1), float(match.group(2))
        try:
            await context.bot.send_invoice(
                chat_id=update.effective_chat.id,
                title=f"Оплата долга {user_data[user_id]['receipt'].payer} от {member}",
                description=f"Оплата долга за покупки: {member} должен {amount:.2f}₽ {user_data[user_id]['receipt'].payer}",
                payload=f"debt_{member}_{amount}",
                provider_token=PAYMENT_PROVIDER_TOKEN,
                currency="RUB",
                prices=[LabeledPrice(f"Долг {user_data[user_id]['receipt'].payer}", int(amount * 100))]
            )
            logger.info(f"Sent invoice for {member} to {user_data[user_id]['receipt'].payer}: {amount:.2f}₽")
        except Exception as e:
            logger.error(f"Error sending invoice: {e}")
            await query.message.reply_text("Ошибка при создании платежа. Проверьте настройки провайдера.")
        return CONFIRMING_ASSIGNMENTS
    
    # Обработка выбора участника
    match = re.match(r"assign_(\d+)_(.+)", data)
    if not match:
        await query.message.reply_text("Ошибка обработки выбора. Попробуйте снова.")
        return CONFIRMING_ASSIGNMENTS
    
    product_index = int(match.group(1))
    selection = match.group(2)
    
    if product_index != user_data[user_id]["current_product_index"]:
        await query.message.reply_text("Ошибка: продукт не соответствует текущему.")
        return CONFIRMING_ASSIGNMENTS
    
    if user_data[user_id]["csv_products"][product_index].get("type") == "shared":
        await query.message.reply_text("Для общих товаров участники фиксированы (все). Измените тип на индивидуальный, если нужно выбрать участников.")
        return CONFIRMING_ASSIGNMENTS
    
    if product_index not in user_data[user_id]["product_assignments"]:
        user_data[user_id]["product_assignments"][product_index] = []
    
    if selection == "shared":
        if user_data[user_id]["product_assignments"][product_index] == user_data[user_id]["members"]:
            user_data[user_id]["product_assignments"][product_index] = []
        else:
            user_data[user_id]["product_assignments"][product_index] = user_data[user_id]["members"].copy()
    elif selection in user_data[user_id]["members"]:
        if selection in user_data[user_id]["product_assignments"][product_index]:
            user_data[user_id]["product_assignments"][product_index].remove(selection)
        else:
            user_data[user_id]["product_assignments"][product_index].append(selection)
    
    logger.info(f"Updated assignments for product {product_index}: {user_data[user_id]['product_assignments'][product_index]}")
    return await show_product_list(update, context)

def parse_qr_data(qr_text):
    params = {}
    for part in qr_text.split('&'):
        if '=' in part:
            key, value = part.split('=', 1)
            params[key] = value
    return params

async def get_receipt_from_fns(qr_text):
    try:
        payload = {"token": FNS_API_KEY, "qrraw": qr_text}
        response = requests.post(FNS_API_URL, data=payload)
        response.raise_for_status()
        
        data = response.json()
        if data.get("code") == 1 and "data" in data and "json" in data["data"] and "document" in data["data"]["json"] and "receipt" in data["data"]["json"]["document"] and "items" in data["data"]["json"]["document"]["receipt"]:
            items = data["data"]["json"]["document"]["receipt"]["items"]
            for item in items:
                if 'price' in item:
                    item['price'] /= 100.0
                if 'sum' in item:
                    item['sum'] /= 100.0
            return items
        return None
    except Exception as e:
        logger.error(f"Error getting receipt from FNS: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        return None

async def decode_qr_from_image(image_path):
    try:
        img = cv2.imread(image_path)
        decoded_objects = decode(img)
        if decoded_objects:
            return decoded_objects[0].data.decode('utf-8')
        return None
    except Exception as e:
        logger.error(f"Error decoding QR from image: {e}")
        return None

async def process_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if update.message.photo:
        await update.message.reply_text("Обрабатываю изображение...")
        photo_file = await update.message.photo[-1].get_file()
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp_file:
            await photo_file.download_to_drive(tmp_file.name)
            qr_text = await decode_qr_from_image(tmp_file.name)
            os.unlink(tmp_file.name)
        
        if not qr_text:
            await update.message.reply_text(
                "Не удалось распознать QR-код. Попробуйте еще раз или введите данные вручную.",
                reply_markup=ReplyKeyboardMarkup([["Добавить продукт", "Сканировать QR-код", "Загрузить CSV"], ["Завершить расчет"]], one_time_keyboard=True)
            )
            return ADDING_PRODUCT_NAME
        text = qr_text
    else:
        text = update.message.text
    
    try:
        qr_data = parse_qr_data(text)
        required_fields = ['t', 's', 'fn', 'i', 'fp']
        if all(field in qr_data for field in required_fields):
            await update.message.reply_text("Получаю данные чека...")
            items = await get_receipt_from_fns(text)
            if items:
                user_data[user_id]["csv_products"].extend([
                    {"name": item['name'], "price": item['price'], "quantity": item.get('quantity', 1), "type": "individual"}
                    for item in items
                ])
                items_list = "\n".join([f"{item['name']} - {item['price']:.2f}₽ x {item.get('quantity', 1)}" for item in items])
                keyboard = [["Добавить продукт", "Сканировать QR-код", "Загрузить CSV"], ["Завершить расчет"]]
                await update.message.reply_text(
                    f"Добавлены товары из чека:\n{items_list}\n\n"
                    "Теперь вы можете распределить их как общие или индивидуальные.",
                    reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
                )
                return ADDING_PRODUCT_NAME
            else:
                await update.message.reply_text(
                    "Не удалось получить данные чека. Попробуйте другой QR-код или добавьте товары вручную.",
                    reply_markup=ReplyKeyboardMarkup([["Добавить продукт", "Сканировать QR-код", "Загрузить CSV"], ["Завершить расчет"]], one_time_keyboard=True)
                )
                return ADDING_PRODUCT_NAME
        else:
            await update.message.reply_text(
                "Неверный формат QR-кода. Попробуйте еще раз или добавьте товары вручную.",
                reply_markup=ReplyKeyboardMarkup([["Добавить продукт", "Сканировать QR-код", "Загрузить CSV"], ["Завершить расчет"]], one_time_keyboard=True)
            )
            return ADDING_PRODUCT_NAME
    except Exception as e:
        logger.error(f"Error processing QR code: {e}")
        await update.message.reply_text(
            "Ошибка при обработке QR-кода. Попробуйте еще раз или добавьте товары вручную.",
            reply_markup=ReplyKeyboardMarkup([["Добавить продукт", "Сканировать QR-код", "Загрузить CSV"], ["Завершить расчет"]], one_time_keyboard=True)
        )
        return ADDING_PRODUCT_NAME

async def process_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not update.message.document:
        await update.message.reply_text(
            "Пожалуйста, отправьте CSV файл.",
            reply_markup=ReplyKeyboardMarkup([["Добавить продукт", "Сканировать QR-код", "Загрузить CSV"], ["Завершить расчет"]], one_time_keyboard=True)
        )
        return PROCESSING_CSV
    
    document = update.message.document
    if not document.file_name.endswith('.csv'):
        await update.message.reply_text(
            "Файл должен быть в формате CSV. Попробуйте еще раз.",
            reply_markup=ReplyKeyboardMarkup([["Добавить продукт", "Сканировать QR-код", "Загрузить CSV"], ["Завершить расчет"]], one_time_keyboard=True)
        )
        return PROCESSING_CSV
    
    await update.message.reply_text("Обрабатываю CSV файл...")
    file = await document.get_file()
    tmp_file_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.csv', delete=False) as tmp_file:
            tmp_file_path = tmp_file.name
            await file.download_to_drive(tmp_file_path)
            with open(tmp_file_path, 'r', encoding='utf-8-sig') as f:
                lines = f.readlines()
                logger.info(f"First 5 lines of CSV: {lines[:5]}")
                
                header_index = None
                for i, line in enumerate(lines):
                    line_clean = line.strip().lower()
                    if 'товар' in line_clean and 'цена' in line_clean:
                        header_index = i
                        break
                
                if header_index is None:
                    logger.error(f"Header row with 'Товар' and 'Цена' not found in CSV")
                    await update.message.reply_text(
                        "Не удалось найти заголовки 'Товар' и 'Цена'. Проверьте формат.",
                        reply_markup=ReplyKeyboardMarkup([["Добавить продукт", "Сканировать QR-код", "Загрузить CSV"], ["Завершить расчет"]], one_time_keyboard=True)
                    )
                    return PROCESSING_CSV
                
                csv_content = ''.join(lines[header_index:])
                csv_file = io.StringIO(csv_content)
                reader = csv.DictReader(csv_file, delimiter=';')
                
                required_fields = ['Товар', 'Цена']
                logger.info(f"Found headers: {reader.fieldnames}")
                if not all(field in reader.fieldnames for field in required_fields):
                    await update.message.reply_text(
                        f"CSV должен содержать колонки 'Товар' и 'Цена'. Найдены: {', '.join(reader.fieldnames or [])}.",
                        reply_markup=ReplyKeyboardMarkup([["Добавить продукт", "Сканировать QR-код", "Загрузить CSV"], ["Завершить расчет"]], one_time_keyboard=True)
                    )
                    return PROCESSING_CSV
                
                user_data[user_id]["csv_products"] = []
                for row in reader:
                    try:
                        name = row['Товар'].strip().strip('"')
                        price = float(row['Цена'].replace(',', '.'))
                        quantity = float(row.get('Количество', '1').replace(',', '.')) if row.get('Количество') else 1
                        if not name or price <= 0:
                            continue
                        user_data[user_id]["csv_products"].append({
                            "name": name,
                            "price": price,
                            "quantity": quantity,
                            "type": "individual"
                        })
                    except (ValueError, KeyError) as e:
                        logger.warning(f"Skipping invalid row: {row}, Error: {e}")
                        continue
                
                if not user_data[user_id]["csv_products"]:
                    await update.message.reply_text(
                        "Не удалось добавить товары из CSV. Проверьте формат данных.",
                        reply_markup=ReplyKeyboardMarkup([["Добавить продукт", "Сканировать QR-код", "Загрузить CSV"], ["Завершить расчет"]], one_time_keyboard=True)
                    )
                    return ADDING_PRODUCT_NAME
                
                return await show_product_list(update, context)
                
    except Exception as e:
        logger.error(f"Error processing CSV: {e}")
        await update.message.reply_text(
            "Ошибка при обработке CSV. Попробуйте еще раз.",
            reply_markup=ReplyKeyboardMarkup([["Добавить продукт", "Сканировать QR-код", "Загрузить CSV"], ["Завершить расчет"]], one_time_keyboard=True)
        )
        return PROCESSING_CSV
    finally:
        if tmp_file_path and os.path.exists(tmp_file_path):
            try:
                os.unlink(tmp_file_path)
            except Exception as e:
                logger.error(f"Error deleting temp file {tmp_file_path}: {e}")

async def send_long_message(message, text: str, max_length: int = 4000):
    if len(text) <= max_length:
        await message.reply_text(text, reply_markup=ReplyKeyboardRemove())
        return
    
    parts = []
    current_part = ""
    for line in text.split('\n'):
        if len(current_part) + len(line) + 1 > max_length:
            parts.append(current_part.strip())
            current_part = line + '\n'
        else:
            current_part += line + '\n'
    if current_part:
        parts.append(current_part.strip())
    
    for part in parts:
        try:
            await message.reply_text(part, reply_markup=ReplyKeyboardRemove() if part == parts[-1] else None)
        except BadRequest as e:
            logger.error(f"Failed to send message part: {e}")
            await message.reply_text(
                "Ошибка при отправке части сообщения.",
                reply_markup=ReplyKeyboardRemove()
            )

async def calculate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not user_data[user_id]["receipt"].items and not user_data[user_id]["receipt"].shared_items:
        message = update.message or update.callback_query.message
        await message.reply_text(
            "Не добавлено ни одного продукта!",
            reply_markup=ReplyKeyboardMarkup([["Добавить продукт", "Сканировать QR-код", "Загрузить CSV"], ["Завершить расчет"]], one_time_keyboard=True)
        )
        return ADDING_PRODUCT_NAME
        
    try:
        message = update.message or update.callback_query.message
        calculation_result, debts = user_data[user_id]["receipt"].calculate(user_data[user_id]["members"])
        verification_list = user_data[user_id]["receipt"].generate_verification_list(user_data[user_id]["members"])
        final_message = f"{calculation_result}\n{verification_list}"
        
        # Генерация круговой диаграммы
        if debts:
            labels = [member for member, _ in debts]
            amounts = [amount for _, amount in debts]
            plt.figure(figsize=(6, 6))
            plt.pie(amounts, labels=labels, autopct='%1.1f%%', startangle=90)
            plt.title("Распределение расходов")
            chart_path = f"chart_{user_id}.png"
            plt.savefig(chart_path)
            plt.close()
            logger.info(f"Generated expense chart: {chart_path}")
        
        # Отправка сообщения и диаграммы
        await send_long_message(message, final_message)
        if debts:
            with open(chart_path, 'rb') as f:
                await message.reply_photo(
                    photo=f,
                    caption="Распределение расходов",
                    reply_markup=ReplyKeyboardRemove()
                )
            os.unlink(chart_path)
        
        # Кнопки для оплаты долгов
        buttons = [
            [InlineKeyboardButton(
                f"Оплатить {user_data[user_id]['receipt'].payer} ({amount:.2f}₽) от {member}",
                callback_data=f"pay_{member}_{amount:.2f}"
            )]
            for member, amount in debts
        ]
        if buttons:
            await message.reply_text(
                "Оплатите долг, если хотите:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        
        # Отправка CSV
        csv_content = user_data[user_id]["receipt"].generate_verification_csv(user_data[user_id]["members"])
        logger.info(f"CSV content (first 200 chars): {csv_content[:200]}")
        
        with tempfile.NamedTemporaryFile(suffix='.csv', delete=False, mode='w', encoding='utf-8') as tmp_file:
            tmp_file.write('\ufeff')
            tmp_file.write(csv_content)
            tmp_file_path = tmp_file.name
        
        try:
            with open(tmp_file_path, 'rb') as f:
                await message.reply_document(
                    document=f,
                    filename='receipt_details.csv',
                    caption="Детализация расчета в CSV",
                    reply_markup=ReplyKeyboardRemove()
                )
        finally:
            os.unlink(tmp_file_path)
        
    except BadRequest as e:
        logger.error(f"Error sending message: {e}")
        message = update.message or update.callback_query.message
        await message.reply_text(
            "Ошибка: сообщение слишком длинное или содержит некорректные данные.",
            reply_markup=ReplyKeyboardRemove()
        )
    except Exception as e:
        logger.error(f"Error generating or sending CSV/chart: {e}")
        message = update.message or update.callback_query.message
        await message.reply_text(
            "Ошибка при создании или отправке данных.",
            reply_markup=ReplyKeyboardRemove()
        )
    
    if user_id in user_data:
        del user_data[user_id]
        
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_data:
        del user_data[user_id]
        
    await update.message.reply_text("Отменено", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def main():
    application = Application.builder().token("TOKEN").build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            SELECTING_ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_action)],
            ADDING_MEMBERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_members)],
            SELECTING_PAYER: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_payer)],
            ADDING_PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_name)],
            ADDING_PRODUCT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_price)],
            SELECTING_PRODUCT_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_product_type)],
            PROCESSING_QR: [MessageHandler(filters.TEXT | filters.PHOTO, process_qr)],
            PROCESSING_CSV: [MessageHandler(filters.Document.ALL, process_csv)],
            CONFIRMING_ASSIGNMENTS: [CallbackQueryHandler(handle_assignment)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    application.add_handler(conv_handler)
    application.run_polling()

if __name__ == '__main__':
    main()