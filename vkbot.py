import vk_api  # pip install vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.utils import get_random_id
from vk_api import VkUpload
from vk_api.keyboard import VkKeyboard
import threading
import logging
import time
import queue
import base64
import io


class VkBot:
    def __init__(self, token: str, group_id: int):
        self.vk_session = vk_api.VkApi(token=token)
        self.vk = self.vk_session.get_api()
        self.upload = VkUpload(self.vk_session)
        self.longpoll = VkBotLongPoll(self.vk_session, group_id)
        self._command_handlers = {}
        self._unknown_command_handler = None  # Обработчик для неизвестных команд
        self._worker_queue = queue.Queue()  # Очередь для задач
        self._stop_event = threading.Event()  # Для остановки потоков
        logging.info(f"Бот инициализирован для группы ID: {group_id}")

    def message_handler(self, commands: list[str]):
        # Декоратор для регистрации обработчиков известных команд
        def decorator(func):
            for cmd in commands:
                self._command_handlers[cmd.lower()] = func
                logging.debug(f"Зарегистрирован обработчик для команды: /{cmd}")
            return func
        return decorator

    def send_stringio_file(self, peer_id, stringio_data, text):
        try:
            doc_upload_result = self.upload.document(stringio_data, title=text, message_peer_id=peer_id)
            doc = doc_upload_result['doc']
            attachment = f"doc{doc['owner_id']}_{doc['id']}"
            self.vk.messages.send(peer_id=peer_id, message=text, random_id=get_random_id(), attachment=attachment)
            stringio_data.close()
        except vk_api.exceptions.ApiError as e:
            logging.error(f"Ошибка при загрузке или отправке файла: {e}", exc_info=True)

    def send_location(self, peer_id, text, lat, long):
        try:
            self.vk.messages.send(peer_id=peer_id, message=text, random_id=get_random_id(), lat=lat, long=long)
            logging.info(f"Геопозиция отправлено в {peer_id}: '{lat} {long}...'")
        except Exception as e:
            logging.error(f"Ошибка при отправке геопозиции в {peer_id}: {e}")

    def send_photo_from_base64(self, base64_data, peer_id_for_upload, image_text):
        try:
            image_data = base64.b64decode(base64_data)
            photo_file = io.BytesIO(image_data)
            photo_file.name = 'image.png'
            photo = self.upload.photo_messages(photos=photo_file, peer_id=peer_id_for_upload)[0]
            attachment = f"photo{photo['owner_id']}_{photo['id']}"
            self.vk.messages.send(peer_id=peer_id_for_upload, message=image_text, random_id=get_random_id(), attachment=attachment)
        except Exception as e:
            print(f"Ошибка при загрузке фотографии: {e}")
            return None

    def send_answer(self):
        # Декоратор для регистрации обработчика неизвестных команд (для отправки кодов). Может быть только один такой обработчик.
        def decorator(func):
            if self._unknown_command_handler:
                logging.warning("Предупреждение: Обработчик неизвестных команд уже зарегистрирован. Перезаписываю.")
            self._unknown_command_handler = func
            logging.debug(f"Зарегистрирован обработчик для неизвестных команд: {func.__name__}")
            return func
        return decorator

    def send_message(self, peer_id: int, message: str):
        # Отправляет сообщение в чат
        try:
            self.vk.messages.send(peer_id=peer_id, message=message, random_id=get_random_id())
        except Exception as e:
            logging.error(f"Ошибка при отправке сообщения в {peer_id}: {e}")


    def send_keyboard(self, peer_id: int):
        keyboard = VkKeyboard(one_time=False)
        keyboard.add_button('/sector')
        keyboard.add_button('/bonus')
        keyboard.add_button('/task')
        keyboard.add_line()
        keyboard.add_button('/hint')
        keyboard.add_button('/screen')
        keyboard.add_button('/del_kb')
        self.vk.messages.send(peer_id=peer_id, message='кнопки добавлены', keyboard=keyboard.get_keyboard(), random_id=get_random_id())


    def remove_keyboard(self, peer_id: int):
        self.vk.messages.send(peer_id=peer_id, message='кнопки убраны', keyboard=VkKeyboard().get_empty_keyboard(), random_id=get_random_id())

    def _process_message(self, event):
        # Обрабатывает входящее сообщение и ставит задачу в очередь
        message = event.obj.message
        text = message.get('text', '').strip()
        #Если сначала обращение к боту, то отбрасываем его
        if text.startswith('['):
            text = text.split(maxsplit=1)[1]

        if text.startswith('/'):
            parts = text[1:].split(maxsplit=1)
            cmd = parts[0].lower()
            handler = self._command_handlers.get(cmd)
            if handler:
                logging.info(f"Постановка задачи в очередь для известной команды /{cmd}")
                self._worker_queue.put((handler, message))
            elif self._unknown_command_handler:  # <-- НОВОЕ: Если команда не найдена, и есть обработчик неизвестных команд
                logging.info(f"Постановка задачи в очередь для неизвестной команды /{cmd}")
                # Передаем полный текст команды (например, "unknown_cmd") в качестве аргумента
                self._worker_queue.put((self._unknown_command_handler, message))
            else:
                self.send_message(message['peer_id'], f"Неизвестная команда: /{cmd}")

    def _worker(self):
        #  Функция, выполняющая задачи из очереди в отдельном потоке
        logging.info("Запущен рабочий поток.")
        while not self._stop_event.is_set():
            try:
                handler, message = self._worker_queue.get(timeout=1)  # Ждем задачу 1 секунду
                logging.info(f"Выполнение задачи: {handler.__name__}")
                try:
                    handler(message)  # Выполняем обработчик
                except Exception as e:
                    logging.error(f"Ошибка в обработчике {handler.__name__}: {e}", exc_info=True)
                    peer_id = message['peer_id']
                    self.send_message(peer_id, "Произошла ошибка при выполнении команды.")
                finally:
                    self._worker_queue.task_done()  # Уведомляем очередь о завершении задачи
            except queue.Empty:
                # Если очередь пуста, просто продолжаем цикл
                pass
            except Exception as e:
                logging.error(f"Ошибка в рабочем потоке: {e}", exc_info=True)
                time.sleep(1)  # Не зацикливаемся при ошибке

        logging.info("Рабочий поток завершен.")

    def run(self):
        # Запускает LongPoll-цикл и рабочий поток
        logging.info("Бот запущен и слушает события...")

        # Запускаем рабочий поток
        worker_thread = threading.Thread(target=self._worker, daemon=True)
        worker_thread.start()

        try:
            for event in self.longpoll.listen():
                if self._stop_event.is_set():
                    break
                if event.type == VkBotEventType.MESSAGE_NEW and event.obj.message.get('text'):
                    self._process_message(event)
        except KeyboardInterrupt:
            logging.info("Бот остановлен вручную.")
        except Exception as e:
            logging.critical(f"Критическая ошибка работы бота: {e}", exc_info=True)
        finally:
            self._stop_event.set()
            self._worker_queue.join()
            worker_thread.join()
            logging.info("Бот завершил работу.")