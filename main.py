import asyncio
import random
import datetime
from typing import Union
from vkbottle.bot import Bot, Message
from vkbottle.tools import DocMessagesUploader
from vkbottle.dispatch.rules import ABCRule
from vkbottle import Keyboard, Text, EMPTY_KEYBOARD
import os.path
import configparser
import logging
from selenium import webdriver  # pip install selenium
from selenium.webdriver.firefox.options import Options
import base64
import io
import json
import re
from bs4 import BeautifulSoup  # pip install BeautifulSoup4
import aiohttp

# Читаем конфиг
try:
    config = configparser.ConfigParser()
    config.read('settings.ini', encoding='utf-8')
    ADMIN_USERNAMES = tuple(config['Settings']['Admins'].split(','))  # Администраторы, которым разрешена авторизация бота в чате
    SECTORS_LEFT_ALERT = int(config['Settings']['Sectors_left_alert'])  # Количество оставшихся для закрытия секторов, с которого выводить оповещение, сколько осталось
    USER_AGENT = 'Temig vk enbot'  # Выставляемый в requests и selenium user-agent
    TASK_MAX_LEN = int(config['Settings']['Task_max_len'])  # Максимальное кол-во символов в одном сообщении, если превышает, то разбивается на несколько
    LANG = config['Settings']['Lang']
    CHECK_INTERVAL = int(config['Settings']['Check_interval'])
    TIMELEFT_ALERT1 = int(config['Settings']['Timeleft_alert1'])
    TIMELEFT_ALERT2 = int(config['Settings']['Timeleft_alert2'])
    VK_GROUP_ID = int(config['Settings']['Vk_group_id'])
    VK_TOKEN = config['Settings']['Vk_token']
    STOP_ACCEPT_CODES_WORDS = tuple(config['Settings']['Stop_accept_codes_words'].split(','))
    USE_BROWSER = True if config['Settings']['Use_browser'].lower() == 'true' else False

except Exception as se:
    print(f'Error reading settings.ini config: {se}')
    exit(1)

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

folder_path = os.path.join(os.curdir, 'level_snapshots')
if not os.path.exists(folder_path):
    os.makedirs(folder_path)

if not globals().get('VK_TOKEN'):
    logging.error("Пожалуйста, установите переменную VK_TOKEN в settings.ini")
    exit(1)

CUR_PARAMS = {}                 # словарь с текущими состояниями слежения в чатах

# Создаем экземпляр бота
BOT = Bot(token=VK_TOKEN)
doc_uploader = DocMessagesUploader(BOT.api)


class MyRule(ABCRule[Message]):
    def __init__(self, commands: list[str], args_count: Union[list[int], None]):
        self.commands = commands
        self.args_count = args_count

    async def check(self, event: Message) -> Union[dict, bool]:
        # VK повторно отправляет сообщение, вставляя ссылку как attachments, второе сообщение не обрабатываем
        if event.attachments:
            return False

        if not event.text:
            return False

        if event.text.startswith(f'[club{VK_GROUP_ID}|@club{VK_GROUP_ID}] '):
            event.text = event.text.split(maxsplit=1)[1]

        # Проверка, что первый символ /
        if event.text[0] != '/':
            return False
        input_split = event.text.split()
        # Проверка, что количество аргументов соответствует
        if self.args_count and len(input_split)-1 not in self.args_count:
            return False
        command = input_split[0][1:].lower()
        # Проверка, что команда соответствует списку команд
        if command not in self.commands:
            return False
        args = input_split[1:] if len(input_split) > 1 else None
        peer_id = event.peer_id
        return {'command': command, 'args': args, 'peer_id': peer_id}


# Функции
def get_cookie(cookie_name, session):
    for cookie in session.cookie_jar:
        if cookie.key == cookie_name:
            return cookie.value


async def send_screen(peer_id, link, full=False):
    if CUR_PARAMS[peer_id]['driver']:
        CUR_PARAMS[peer_id]['driver'].get(link)
        if full:
            img_buffer = io.BytesIO(base64.b64decode(CUR_PARAMS[peer_id]['driver'].get_full_page_screenshot_as_base64()))
        else:
            img_buffer = io.BytesIO(base64.b64decode(CUR_PARAMS[peer_id]['driver'].get_screenshot_as_base64()))
        img_buffer.name = 'screen_file.png'
        attachment = await doc_uploader.upload(file_source=img_buffer, peer_id=peer_id, filename=img_buffer.name, title=img_buffer.name)
        await BOT.api.messages.send(peer_id=peer_id, message='', attachment=attachment, random_id=random.getrandbits(32))

    else:
        await BOT.api.messages.send(peer_id=peer_id, message='Виртуальный браузер не запущен', random_id=random.getrandbits(32))


def parse_html(html_content):
    try:
        soup = BeautifulSoup(html_content, 'lxml')
        for img_tag in soup.find_all('img'):
            src = img_tag.get('src')
            if src:
                inline_image_text = f"[Img: {src}]"
                img_tag.replace_with(inline_image_text + " ")
            else:
                img_tag.decompose()

        for br_tag in soup.find_all(['br', 'br/']):
            br_tag.replace_with('\n')

        for a_tag in soup.find_all('a'):
            href = a_tag.get('href')
            link_text = a_tag.get_text(strip=True)
            if href and link_text:
                inline_link_text = f"[{link_text}]({href})"
                a_tag.replace_with(inline_link_text)
            else:
                a_tag.replace_with(link_text)

        text_content = soup.get_text()
    except Exception as e:
        text_content = f'Ошибка парсинга текста: {e} \n {html_content}'

    return text_content


async def send_parsed_message(peer_id, message, parse_html_flag=False):
    try:
        if parse_html_flag:
            new_message = parse_html(message)
        else:
            new_message = message
        await BOT.api.messages.send(peer_id=peer_id, message=new_message, random_id=random.getrandbits(32))

    except Exception as e:
        logging.error(f"Ошибка при отправке сообщения в {peer_id}: {e}")


# Отправить информацию о текущем уровне
async def send_curlevel_info(cur_chat, cur_json):
    # Выводим информацию о номере уровня, автопереходе, блокировке ответов
    gameinfo_str = f'Уровень {cur_json["Level"]["Number"]} из {len(cur_json["Levels"])} {cur_json["Level"]["Name"]}\n'
    gameinfo_str += f'Выполнить секторов: {cur_json["Level"]["RequiredSectorsCount"] if cur_json["Level"]["RequiredSectorsCount"] > 0 else 1} из {len(cur_json["Level"]["Sectors"]) if len(cur_json["Level"]["Sectors"]) > 0 else 1}\n'
    if cur_json["Level"]["Messages"]:
        gameinfo_str += 'Сообщения на уровне:\n'
        for elem in cur_json["Level"]["Messages"]:
            gameinfo_str += elem["MessageText"]+'\n'

    if cur_json["Level"]["Timeout"] > 0:
        gameinfo_str += f'Автопереход через {datetime.timedelta(seconds=cur_json["Level"]["Timeout"])}\n'
    else:
        gameinfo_str += 'Автопереход отсутствует\n'
    if cur_json["Level"]["HasAnswerBlockRule"]:
        gameinfo_str += f'ВНИМАНИЕ, БЛОКИРОВКА ОТВЕТОВ! НЕ БОЛЕЕ {cur_json["Level"]["AttemtsNumber"]} ПОПЫТОК ЗА {datetime.timedelta(seconds=cur_json["Level"]["AttemtsPeriod"])} ДЛЯ {"КОМАНДЫ" if cur_json["Level"]["BlockTargetId"] == 2 else "ИГРОКА"}'
    await send_parsed_message(cur_chat, gameinfo_str, CUR_PARAMS[cur_chat]['parser'])

    # Отдельно выводим задание
    if len(cur_json['Level']['Tasks']) > 0:
        gamelevel_str = cur_json['Level']['Tasks'][0]['TaskText']
    else:
        gamelevel_str = 'Нет заданий на уровне'

    # Если очень большой текст на уровне, то сплит
    for i in range(0, len(gamelevel_str), TASK_MAX_LEN):
        await send_parsed_message(cur_chat, gamelevel_str[i:i + TASK_MAX_LEN], CUR_PARAMS[cur_chat]['parser'])


async def check_engine(cur_chat_id):
    try:
        async with CUR_PARAMS[cur_chat_id]["session"].get(f'https://{CUR_PARAMS[cur_chat_id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[cur_chat_id]["cur_json"]["GameId"]}?json=1&lang={LANG}') as response:
            response.raise_for_status()
            game_json = await response.json()
    except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as CE:
        print(f'Ошибка соединения {CE}, переподключаюсь')
        return True

    except Exception as e:
        await BOT.api.messages.send(peer_id=cur_chat_id, message=f'Ошибка мониторинга, возможно необходимо заново авторизоваться: {e}', random_id=random.getrandbits(32))
        logging.error(f"Ошибка мониторинга бота: {e}", exc_info=True)
        return

        # False - если цикл надо прервать (Серьезная ошибка), True - если продолжать
    match game_json['Event']:
        case 2:
            await BOT.api.messages.send(peer_id=cur_chat_id, message='Игра с указанным id не существует', random_id=random.getrandbits(32))
            return
        case 4:
            await BOT.api.messages.send(peer_id=cur_chat_id, message='Ошибка авторизации', random_id=random.getrandbits(32))
            return
        case 5:
            print("Game hasn't started yet, continue monitoring")
            return True  # игра еще не началась, продолжаем мониторить
        case 6 | 17:
            await BOT.api.messages.send(peer_id=cur_chat_id, message='Игра закончилась', random_id=random.getrandbits(32))
            CUR_PARAMS[cur_chat_id]['monitoring_flag'] = False
            await asyncio.sleep(7)
            await BOT.api.messages.send(peer_id=cur_chat_id, message='Авторизация чата отключена', random_id=random.getrandbits(32))
            CUR_PARAMS.pop(cur_chat_id, None)  # Освобождаем в памяти словарь чата
            return
        case 7 | 8:
            await BOT.api.messages.send(peer_id=cur_chat_id, message='Заявка не подана', random_id=random.getrandbits(32))
            return
        case 9:
            await BOT.api.messages.send(peer_id=cur_chat_id, message='Команда не принята в игру', random_id=random.getrandbits(32))
            return
        case 10:
            await BOT.api.messages.send(peer_id=cur_chat_id, message='Аккаунт не в команде', random_id=random.getrandbits(32))
            return
        case 11:
            await BOT.api.messages.send(peer_id=cur_chat_id, message='Аккаунт не активен в команде', random_id=random.getrandbits(32))
            return
        case 12:
            await BOT.api.messages.send(peer_id=cur_chat_id, message='Игра не содержит уровней', random_id=random.getrandbits(32))
            return
        case 13:
            await BOT.api.messages.send(peer_id=cur_chat_id, message='Превышено количество участников', random_id=random.getrandbits(32))
            return
        case 16 | 18 | 21:
            await BOT.api.messages.send(peer_id=cur_chat_id, message='Уровень был снят', random_id=random.getrandbits(32))
            await check_engine(cur_chat_id)
            return True
        case 19 | 22:
            await BOT.api.messages.send(peer_id=cur_chat_id, message='Уровень пройден по автопереходу', random_id=random.getrandbits(32))
            await check_engine(cur_chat_id)
            return True
        case 20:
            await check_engine(cur_chat_id)
            return True  # все секторы выполнены
        case 0:
            old_json = CUR_PARAMS[cur_chat_id]['cur_json']  # предыдущий json
            CUR_PARAMS[cur_chat_id]['cur_json'] = game_json  # текущий json

            # Игра началась
            if old_json['Level'] is None:
                await BOT.api.messages.send(peer_id=cur_chat_id, message='Игра началась!\n', random_id=random.getrandbits(32))
                await send_curlevel_info(cur_chat_id, game_json)
                return True

            # Проверка, что поменялся номер уровня, т.е. произошел АП
            if old_json['Level']['Number'] != game_json['Level']['Number']:
                CUR_PARAMS[cur_chat_id]['5_min_sent'] = False
                CUR_PARAMS[cur_chat_id]['1_min_sent'] = False
                await BOT.api.messages.send(peer_id=cur_chat_id, message='АП!\n' + ' '.join(CUR_PARAMS[cur_chat_id].get('players', '')), random_id=random.getrandbits(32))
                if CUR_PARAMS[cur_chat_id]['send_screen']:
                    await send_screen(cur_chat_id, f'https://{CUR_PARAMS[cur_chat_id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[cur_chat_id]["cur_json"]["GameId"]}?lang={LANG}', full=True)

                # отключение ввода кодов при обнаружении штрафных
                if len(game_json['Level']['Tasks']) > 0:
                    if any(item in game_json['Level']['Tasks'][0]['TaskText'].lower() for item in STOP_ACCEPT_CODES_WORDS):
                        CUR_PARAMS[cur_chat_id]['accept_codes'] = False
                        await BOT.api.messages.send(peer_id=cur_chat_id, message='В тексте обнаружена информация о штрафах или ложных кодах, ввод кодов отключен! Для включения выполните /accept_codes', random_id=random.getrandbits(32))

                await send_curlevel_info(cur_chat_id, game_json)

                if len(game_json['Level']['Tasks']) > 0:
                    await send_kml_info(cur_chat_id, game_json['Level']['Tasks'][0]['TaskText'], game_json['Level']['Number'])

                # Сохраняем информацию о пройденном уровне
                CUR_PARAMS[cur_chat_id]['OLD_LEVELS'][str(old_json['Level']['Number'])] = {}
                CUR_PARAMS[cur_chat_id]['OLD_LEVELS'][str(old_json['Level']['Number'])]['Event'] = old_json['Event']
                CUR_PARAMS[cur_chat_id]['OLD_LEVELS'][str(old_json['Level']['Number'])]['Level'] = old_json['Level']

                # Запись в файл
                json_file_data = CUR_PARAMS[cur_chat_id]['OLD_LEVELS']
                json_filename = f'{cur_chat_id}.{CUR_PARAMS[cur_chat_id]["cur_json"]["GameId"]}'
                if os.path.isfile('level_snapshots/' + json_filename):
                    with open('level_snapshots/' + json_filename) as json_file:
                        json_file_data.update(json.load(json_file))
                with open('level_snapshots/' + json_filename, 'w') as json_file:
                    json.dump(json_file_data, json_file)
                return True

            # проверка на изменение текста уровня
            if old_json['Level']['Tasks'] != game_json['Level']['Tasks']:
                await BOT.api.messages.send(peer_id=cur_chat_id, message='Задание уровня изменилось', random_id=random.getrandbits(32))

            # проверка на сообщения на уровне:
            for elem in game_json['Level']['Messages']:
                if elem not in old_json['Level']['Messages']:
                    await send_parsed_message(cur_chat_id, f'Добавлено сообщение: {elem["MessageText"]}', CUR_PARAMS[cur_chat_id]['parser'])

            # проверка на количество секторов на уровне:
            if len(old_json['Level']['Sectors']) != len(game_json['Level']['Sectors']):
                await BOT.api.messages.send(peer_id=cur_chat_id, message='Количество секторов на уровне изменилось', random_id=random.getrandbits(32))

            # проверка на количество бонусов на уровне:
            if len(old_json['Level']['Bonuses']) != len(game_json['Level']['Bonuses']):
                await BOT.api.messages.send(peer_id=cur_chat_id, message='Количество бонусов на уровне изменилось', random_id=random.getrandbits(32))

            # проверка на количество необходимых секторов:
            if old_json['Level']['RequiredSectorsCount'] != game_json['Level']['RequiredSectorsCount']:
                await BOT.api.messages.send(peer_id=cur_chat_id, message='Количество необходимых для прохождения секторов изменилось', random_id=random.getrandbits(32))

            # проверка на кол-во оставшихся секторов:
            cur_sectors_left = game_json['Level']['SectorsLeftToClose']
            if old_json['Level']['SectorsLeftToClose'] != cur_sectors_left and cur_sectors_left <= SECTORS_LEFT_ALERT:
                sector_list = [str(elem['Name']) for elem in game_json['Level']['Sectors'] if not (elem['IsAnswered'])]
                await BOT.api.messages.send(peer_id=cur_chat_id, message=f'Осталось секторов: [{cur_sectors_left}]. Оставшиеся: {", ".join(sector_list)}', random_id=random.getrandbits(32))

            # Проверка, что пришла подсказка
            if len(CUR_PARAMS[cur_chat_id]["cur_json"]['Level']['Helps']) != len(old_json['Level']['Helps']):
                await BOT.api.messages.send(peer_id=cur_chat_id, message='Была добавлена подсказка', random_id=random.getrandbits(32))
            else:
                for i, elem in enumerate(CUR_PARAMS[cur_chat_id]["cur_json"]['Level']['Helps']):
                    if elem['HelpText'] != old_json['Level']['Helps'][i]['HelpText']:
                        await send_parsed_message(cur_chat_id, f'Подсказка {i + 1}: {elem["HelpText"]}', CUR_PARAMS[cur_chat_id]['parser'])
                        await send_kml_info(cur_chat_id, elem["HelpText"], f'{CUR_PARAMS[cur_chat_id]["cur_json"]["Level"]["Number"]}_{i + 1}')

            # мониторинг закрытия секторов
            if CUR_PARAMS[cur_chat_id]['sector_monitor']:
                sector_msg = ''
                for elem in game_json['Level']['Sectors']:
                    if elem not in old_json['Level']['Sectors'] and elem["IsAnswered"] and (elem['SectorId'] not in CUR_PARAMS[cur_chat_id]['sector_closers']):
                        sector_msg += f'✅№{elem["Order"]} {elem["Name"]} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]})\n'
                if sector_msg != '':
                    await BOT.api.messages.send(peer_id=cur_chat_id, message=sector_msg, random_id=random.getrandbits(32))

            # мониторинг закрытия бонусов
            if CUR_PARAMS[cur_chat_id]['bonus_monitor']:
                for elem in game_json['Level']['Bonuses']:
                    if elem not in old_json['Level']['Bonuses'] and elem["IsAnswered"] and (elem['BonusId'] not in CUR_PARAMS[cur_chat_id]['sector_closers']):
                        await send_parsed_message(cur_chat_id, f'{"🔴" if elem["Negative"] else "🟢"} №{elem["Number"]} {elem["Name"] or ""} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]}) {"Штраф: " if elem["Negative"] else "Бонус: "} {datetime.timedelta(seconds=elem["AwardTime"])}\n{"Подсказка бонуса:" + chr(10) + elem["Help"] if elem["Help"] else ""}', CUR_PARAMS[cur_chat_id]['parser'])

                        if elem["Help"]:
                            await send_kml_info(cur_chat_id, elem["Help"], CUR_PARAMS[cur_chat_id]["cur_json"]["Level"]["Number"])

            # мониторинг времени до автоперехода
            if TIMELEFT_ALERT1 > game_json['Level']['TimeoutSecondsRemain'] > 0 and not (CUR_PARAMS[cur_chat_id]['5_min_sent']):
                await BOT.api.messages.send(peer_id=cur_chat_id, message='До автоперехода осталось менее 5 минут!', random_id=random.getrandbits(32))
                CUR_PARAMS[cur_chat_id]['5_min_sent'] = True
            if TIMELEFT_ALERT2 > game_json['Level']['TimeoutSecondsRemain'] > 0 and not (CUR_PARAMS[cur_chat_id]['1_min_sent']):
                await BOT.api.messages.send(peer_id=cur_chat_id, message='До автоперехода осталось менее 1 минуты!', random_id=random.getrandbits(32))
                CUR_PARAMS[cur_chat_id]['1_min_sent'] = True
    return True


def gen_kml2(text: str):
    coord_list = re.findall(r'-?\d{1,2}\.\d{3,10}[, ]*-?\d{1,3}\.\d{3,10}', text)
    if not coord_list:
        return None, None
    result_list = []
    kml = '<kml><Document>'
    for cnt, elem in enumerate(coord_list):
        c = re.findall(r'-?\d{1,3}\.\d{3,10}', elem)
        new_point = f'<Point><coordinates>{c[1]},{c[0]},0.0</coordinates></Point>'
        if new_point not in kml:
            kml += f'<Placemark><name>Point {cnt+1}</name>{new_point}</Placemark>'
            result_list.append((c[0], c[1]))
    kml += '</Document></kml>'
    # buf_file = io.StringIO()
    # buf_file.write(kml)
    buf_file = io.BytesIO(kml.encode('utf-8'))
    buf_file.seek(0, 0)
    return buf_file, result_list  # Возвращаем кортеж из файла kml и списка координат


async def send_kml_info(cur_chat, parse_text, level_num):
    kml_file, coords_list = gen_kml2(parse_text)
    if kml_file:
        kml_file.name = f'points{level_num}.kml'
        attachment = await doc_uploader.upload(kml_file, peer_id=cur_chat, filename=kml_file.name, title=kml_file.name)
        await BOT.api.messages.send(peer_id=cur_chat, message='', attachment=attachment, random_id=random.getrandbits(32))
        await BOT.api.messages.send(peer_id=cur_chat, message=f'{coords_list[0][0]}, {coords_list[0][1]}', lat=coords_list[0][0], long=coords_list[0][1], random_id=random.getrandbits(32))


async def monitoring_func(cur_chat_id):
    start_time = datetime.datetime.now()
    await BOT.api.messages.send(peer_id=cur_chat_id, message='Мониторинг включен', random_id=random.getrandbits(32))
    while CUR_PARAMS[cur_chat_id]['monitoring_flag']:
        print(f'Слежение за игрой в чате {cur_chat_id} работает {datetime.datetime.now()-start_time}')
        await asyncio.sleep(CHECK_INTERVAL+random.uniform(-1, 1))
        try:
            if not (await check_engine(cur_chat_id)):
                break
        except Exception as e:
            logging.error(f"Ошибка функции check_engine, продолжаю мониторинг: {e}", exc_info=True)
    CUR_PARAMS[cur_chat_id]['monitoring_flag'] = False
    await BOT.api.messages.send(peer_id=cur_chat_id, message='Мониторинг выключен', random_id=random.getrandbits(32))


# далее команды бота
@BOT.on.message(MyRule(['help', 'start'], [0]))
async def cmd_help(message: Message):
    await message.answer(r'''Temig vk enbot v1.0
https://github.com/temig74
/help, /start - этот help
/auth домен id_игры логин пароль [id_чата] - авторизовать бота на игру в игровом чате (или в личке, добавив id_чата)
/stop_auth - отключить чат
/get_id - получить id чата и пользователя
/game_monitor [0] - включить/[отключить] слежение за игрой
/s, /sector [level№] - показать сектора [прошедшего_уровня]
/sectors_left - оставшиеся сектора на уровне
/b, /bonuses [level№] - показать бонусы [прошедшего_уровня]
/h, /hint - показать подсказки
/t, /task - показать текущее задание
/screen, /скрин - скриншот текущего уровня (необходим firefox)
/fscreen, /фскрин - полный скриншот текущего уровня (необходим firefox)
/любой_код123 - вбитие в движок любой_код123
/accept_codes [0] - включить/[выключить] прием кодов из чата
/sector_monitor [0] - включить/[выключить] мониторинг секторов
/bonus_monitor [0] - включить/[выключить] мониторинг бонусов
/parser [0] - включить/[выключить] парсер HTML
/send_screen [0] - включить/[выключить] отправку скрина нового уровня
/time - оставшееся время до апа
/load_old_json - загрузить информацию о прошедших уровнях игры из файла (при перезапуске бота)
/geo или /* координаты через пробел - отправить геометку по координатам
/set_players @игрок1 @игрок2... - установить список полевых игроков
/open_browser открыть бразуер на компьютере, где запущен бот, привязанный к сессии бота (необходим firefox)
/game_info - информация об игре
/set_doc - установить ссылку на гуглдок
/buttons - добавить клавиатуру с кнопками
/w название_статьи - скрин статьи из вики
/wf название_статьи - полный скрин статьи из вики
''')


@BOT.on.message(MyRule(['auth'], [0, 4, 5]))
async def cmd_auth(message: Message, args: list[str], peer_id: int):
    if str(message.from_id) not in ADMIN_USERNAMES:
        await message.answer('Недостаточно прав для авторизации бота')
        return

    if not args:
        await message.answer('Введите команду в формате /auth домен id_игры логин пароль [id_чата]')

    if len(args) == 5 and args[4].isdigit():
        cur_chat_id = int(args[4])
    elif len(args) == 4:
        cur_chat_id = peer_id
    else:
        await message.answer('Неверный формат id чата')
        return

    if not args[1].isdigit():
        await message.answer('Неверный формат id игры')
        return

    my_domain, my_game_id, my_login, my_password = args[:4]

    # Закрываем старую сессию, если по новой авторизуем в этом же чате
    if session := CUR_PARAMS.get(cur_chat_id, {}).get('session'):
        await session.close()

    my_session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})

    try:
        async with my_session.post(f'https://{my_domain}/login/signin?json=1', data={'Login': my_login, 'Password': my_password}) as response:
            response.raise_for_status()
            auth_request_json = await response.json()
    except Exception as e:
        await message.answer(f'Ошибка запроса авторизации, возможно неверно указан домен:{e}')
        logging.error(f"Ошибка авторизации бота: {e}", exc_info=True)
        return
    match auth_request_json['Error']:
        case 1:
            await message.answer('Превышено количество неправильных попыток авторизации')
            return
        case 2:
            await message.answer('Неверный логин или пароль')
            return
        case 3:
            await message.answer('Пользователь или в Cибири, или в черном списке, или на домене нельзя авторизовываться с других доменов')
            return
        case 4:
            await message.answer('Блокировка по IP')
            return
        case 5:
            await message.answer('В процессе авторизации произошла ошибка на сервере')
            return
        case 6:
            await message.answer('Ошибка')
            return
        case 7:
            await message.answer('Пользователь заблокирован администратором')
            return
        case 8:
            await message.answer('Новый пользователь не активирован')
            return
        case 9:
            await message.answer('Действия пользователя расценены как брутфорс')
            return
        case 10:
            await message.answer('Пользователь не подтвердил e-mail')
            return
        case 0:
            print('Авторизация успешна')
            try:
                # Получаем информацию об игре
                async with my_session.get(f'https://{my_domain}/GameEngines/Encounter/Play/{my_game_id}?json=1') as response:
                    response.raise_for_status()
                    cur_json = await response.json()
            except Exception as e:
                await message.answer(f'Ошибка запроса авторизации, возможно неверно указан id игры: {e}')
                logging.error(f"Ошибка авторизации бота: {e}", exc_info=True)
                return

            await message.answer('Авторизация успешна')  # Только если успешна, то заново инициализируем словарь параметров чата
            CUR_PARAMS[cur_chat_id] = {
                'cur_json': cur_json,
                'session': my_session,
                'cur_domain': my_domain,
                'monitoring_flag': False,
                'accept_codes': True,
                'sector_monitor': True,
                'bonus_monitor': True,
                'send_screen': True,
                'parser': True,
                'route_builder': False,
                '5_min_sent': False,
                '1_min_sent': False,
                'OLD_LEVELS': {},
                'driver': None,
                'sector_closers': {},
                'bonus_closers': {},
                'last_coords': None}

            if USE_BROWSER:
                # запускаем firefox браузер, который будем использовать для скриншотов уровня
                options = Options()
                options.add_argument("--headless")  # не отображаемый в системе
                options.set_preference("general.useragent.override", USER_AGENT)
                my_driver = webdriver.Firefox(options=options)
                my_driver.get(f'https://{my_domain}/GameEngines/Encounter/Play/{my_game_id}')
                my_driver.add_cookie({'name': 'atoken', 'value': get_cookie('atoken', my_session), 'domain': '.' + my_domain, 'secure': False, 'httpOnly': True, 'session': True})
                my_driver.add_cookie({'name': 'stoken', 'value': get_cookie('stoken', my_session), 'domain': '.' + my_domain, 'secure': False, 'httpOnly': False, 'session': True})
                CUR_PARAMS[cur_chat_id]['driver'] = my_driver
                await message.answer('Виртуальный браузер запущен')


@BOT.on.message(MyRule(['screen', 'скрин', 'fscreen', 'фскрин'], [0]))
async def cmd_screen(message: Message, command: str, peer_id: int):
    full = command in ['fscreen', 'фскрин']
    await send_screen(peer_id, f'https://{CUR_PARAMS[peer_id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[peer_id]["cur_json"]["GameId"]}?lang={LANG}', full)


@BOT.on.message(MyRule(['game_monitor'], [0, 1]))
async def cmd_game_monitor(message: Message, args: str, peer_id: int):
    if peer_id not in CUR_PARAMS:
        await message.answer('Не авторизованный чат')
        return
    if args and args[0] == '0':
        CUR_PARAMS[peer_id]['monitoring_flag'] = False
    else:
        if not (CUR_PARAMS[peer_id]['monitoring_flag']):
            CUR_PARAMS[peer_id]['monitoring_flag'] = True
            asyncio.create_task(monitoring_func(peer_id))
        else:
            await message.answer('Слежение уже запущено')


@BOT.on.message(MyRule(['stop_auth'], [0]))
async def cmd_stop_auth(message: Message, peer_id: int):
    if str(message.from_id) not in ADMIN_USERNAMES:
        await message.answer('Недостаточно прав для отключения авторизации бота')
        return

    CUR_PARAMS[peer_id]['monitoring_flag'] = False
    await message.answer('Авторизация чата отключена')
    await CUR_PARAMS[peer_id]['session'].close()
    await asyncio.sleep(7)
    CUR_PARAMS.pop(peer_id, None)  # Освобождаем в памяти словарь чата


@BOT.on.message(MyRule(['get_id'], [0]))
async def cmd_get_id(message: Message, peer_id: int):
    await message.answer(f'id чата: {peer_id}\nid пользователя: {message.from_id}')


@BOT.on.message(MyRule(['h', 'hint'], [0]))
async def cmd_hint(message: Message, peer_id: int):
    try:
        async with CUR_PARAMS[peer_id]['session'].get(f'https://{CUR_PARAMS[peer_id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[peer_id]["cur_json"]["GameId"]}?json=1') as response:
            response.raise_for_status()
            game_json = await response.json()
    except Exception as e:
        await message.answer(f'Ошибка, возможно необходимо заново авторизоваться: {e}')
        logging.error(f"Ошибка, возможно необходимо заново авторизоваться: {e}", exc_info=True)
        return

    if game_json['Event'] != 0:
        await message.answer('Ошибка')
        return

    result_str = ''
    for elem in game_json['Level']['Helps']:
        if elem['RemainSeconds'] == 0:
            result_str += f'Подсказка {elem["Number"]}:\n{elem["HelpText"]}\n{"_"*30}\n\n'
        else:
            result_str += f'Подсказка {elem["Number"]}: Будет через {datetime.timedelta(seconds=elem["RemainSeconds"])}\n{"_"*30}\n\n'
    if result_str == '':
        result_str = 'Нет подсказок'
    await send_parsed_message(peer_id, result_str, CUR_PARAMS[peer_id]['parser'])


@BOT.on.message(MyRule(['t', 'task'], [0]))
async def cmd_task(message: Message, peer_id: int):
    await check_engine(peer_id)
    await send_curlevel_info(peer_id, CUR_PARAMS[peer_id]['cur_json'])
    await cmd_hint(message, peer_id)


@BOT.on.message(MyRule(['open_browser'], [0]))
async def cmd_open_browser(message: Message, peer_id: int):
    if str(message.from_id) not in ADMIN_USERNAMES:
        await message.answer('Недостаточно прав для запуска браузера')
        return
    if USE_BROWSER:
        my_options = Options()
        my_options.set_preference("general.useragent.override", USER_AGENT)
        my_driver = webdriver.Firefox(options=my_options)
        my_driver.get(f'https://{CUR_PARAMS[peer_id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[peer_id]["cur_json"]["GameId"]}')
        my_driver.add_cookie({'name': 'atoken', 'value': get_cookie('atoken', CUR_PARAMS[peer_id]['session']), 'domain': '.' + CUR_PARAMS[peer_id]['cur_domain'], 'secure': False, 'httpOnly': True, 'session': True})
        my_driver.add_cookie({'name': 'stoken', 'value': get_cookie('stoken', CUR_PARAMS[peer_id]['session']), 'domain': '.' + CUR_PARAMS[peer_id]['cur_domain'], 'secure': False, 'httpOnly': False, 'session': True})
        my_driver.get(f'https://{CUR_PARAMS[peer_id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[peer_id]["cur_json"]["GameId"]}')
    else:
        await message.answer('Браузер отключен в конфиге')


@BOT.on.message(MyRule(['time'], [0]))
async def cmd_time(message: Message, peer_id: int):
    try:
        async with CUR_PARAMS[peer_id]['session'].get(f'https://{CUR_PARAMS[peer_id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[peer_id]["cur_json"]["GameId"]}?json=1') as response:
            response.raise_for_status()
            game_json = await response.json()
    except Exception as e:
        await message.answer(peer_id, f'Ошибка, возможно необходимо заново авторизоваться {e}')
        logging.error(f"Ошибка, возможно необходимо заново авторизоваться: {e}", exc_info=True)
        return

    if game_json['Event'] != 0:
        await message.answer('Ошибка')
        return
    if game_json["Level"]["Timeout"] == 0:
        await message.answer('Автопереход отсутствует')
        return
    await message.answer(f'Автопереход через {datetime.timedelta(seconds=game_json["Level"]["TimeoutSecondsRemain"])}')


@BOT.on.message(MyRule(['s', 'sectors', 'sectors_left'], [0, 1]))
async def cmd_sectors(message: Message, command: str, args: list[str], peer_id: int):
    # Если указан номер уровня, то загружаем из OLD_LEVELS
    if args:
        if args[0] in CUR_PARAMS[peer_id]['OLD_LEVELS']:
            game_json = CUR_PARAMS[peer_id]['OLD_LEVELS'][args[0]]
        else:
            await message.answer('Уровень не найден в прошедших')
            return
    else:
        try:
            async with CUR_PARAMS[peer_id]['session'].get(f'https://{CUR_PARAMS[peer_id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[peer_id]["cur_json"]["GameId"]}?json=1') as response:
                response.raise_for_status()
                game_json = await response.json()
        except Exception as e:
            await message.answer(f'Ошибка, возможно необходимо заново авторизоваться: {e}')
            logging.error(f"Ошибка, возможно необходимо заново авторизоваться: {e}", exc_info=True)
            return

    result_str = ''

    if game_json['Event'] != 0:
        await message.answer('Ошибка')
        return

    for elem in game_json['Level']['Sectors']:
        if elem['IsAnswered']:
            if command in ('s', 'sectors'):
                result_str += f'✅№{elem["Order"]} {elem["Name"]} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]}) {CUR_PARAMS[peer_id]["sector_closers"].get(elem["SectorId"], "")}\n'
        else:
            result_str += f'❌№{elem["Order"]} {elem["Name"]}\n'
    if result_str == '':
        result_str = 'Нет секторов'

    result_str = f'Осталось закрыть: {game_json["Level"]["SectorsLeftToClose"] if game_json["Level"]["SectorsLeftToClose"] > 0 else 1} из {len(game_json["Level"]["Sectors"]) if len(game_json["Level"]["Sectors"]) > 0 else 1}\n' + result_str

    for i in range(0, len(result_str), TASK_MAX_LEN):
        await message.answer(result_str[i:i + TASK_MAX_LEN])


@BOT.on.message(MyRule(['b', 'bonuses'], [0, 1]))
async def cmd_bonuses(message: Message, args: list[str], peer_id: int):
    if args:
        if args[0] in CUR_PARAMS[peer_id]['OLD_LEVELS']:
            game_json = CUR_PARAMS[peer_id]['OLD_LEVELS'][args[0]]
        else:
            await message.answer('Уровень не найден в прошедших')
            return
    else:
        try:
            async with CUR_PARAMS[peer_id]['session'].get(f'https://{CUR_PARAMS[peer_id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[peer_id]["cur_json"]["GameId"]}?json=1') as response:
                response.raise_for_status()
                game_json = await response.json()
        except Exception as e:
            await message.answer(f'Ошибка, возможно необходимо заново авторизоваться: {e}')
            logging.error(f"Ошибка, возможно необходимо заново авторизоваться: {e}", exc_info=True)
            return

    result_str = ''

    if game_json['Event'] != 0:
        await message.answer('Ошибка')

    for elem in game_json['Level']['Bonuses']:
        if elem['IsAnswered']:
            result_str += f'{"🔴" if elem["Negative"] else "🟢"}№{elem["Number"]} {elem["Name"] or ""} {elem["Help"] or ""} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]}) {CUR_PARAMS[peer_id]["bonus_closers"].get(elem["BonusId"], "")} {"Штраф: " if elem["Negative"] else "Бонус: "} {datetime.timedelta(seconds=elem["AwardTime"])}\n'
        else:
            result_str += f'{"✖Истёк" if elem["Expired"] else "❌"}№{elem["Number"]} {elem["Name"] or ""} {elem["Task"] or ""} {"Будет доступен через "+str(datetime.timedelta(seconds=elem["SecondsToStart"])) if elem["SecondsToStart"] != 0 else ""} {"Осталось на выполнение: "+str(datetime.timedelta(seconds=elem["SecondsLeft"])) if elem["SecondsLeft"] != 0 else ""}\n'
    if result_str == '':
        result_str = 'Нет бонусов'

    for i in range(0, len(result_str), TASK_MAX_LEN):
        await send_parsed_message(peer_id, result_str[i:i + TASK_MAX_LEN], CUR_PARAMS[peer_id]['parser'])


@BOT.on.message(MyRule(['load_old_json'], [0]))
async def cmd_load_old_json(message: Message, peer_id: int):
    json_filename = str(peer_id) + '.' + str(CUR_PARAMS[peer_id]["cur_json"]["GameId"])
    if os.path.isfile('level_snapshots/'+json_filename):
        with open('level_snapshots/'+json_filename, 'r') as json_file:
            CUR_PARAMS[peer_id]['OLD_LEVELS'].update(json.load(json_file))
    else:
        await message.answer('Файл не существует')


@BOT.on.message(MyRule(['accept_codes', 'sector_monitor', 'bonus_monitor', 'send_screen', 'parser'], [0, 1]))
async def switch_flag(message: Message, command: str, args: list[str], peer_id: int):
    d = {'accept_codes': 'Прием кодов',
         'sector_monitor': 'Мониторинг секторов',
         'bonus_monitor': 'Мониторинг бонусов',
         'send_screen': 'Отправитель скринов',
         'parser': 'Парсер HTML'
         }
    if args and args[0] == '0':
        cmd_flag = False
    else:
        cmd_flag = True
    CUR_PARAMS[peer_id][command] = cmd_flag
    await message.answer(f'{d.get(command)} {"включен" if cmd_flag else "выключен"}')


# список игроков для тегания например при АПе уровня
@BOT.on.message(MyRule(['set_players'], None))
async def cmd_set_players(message: Message, args: list[str], peer_id: int):
    if args:
        CUR_PARAMS[peer_id]['players'] = args
    await message.answer('Список игроков установлен')


@BOT.on.message(MyRule(['set_doc'], [0, 1]))
async def cmd_set_doc(message: Message, args: list[str], peer_id: int):
    if args:
        CUR_PARAMS[peer_id]['doc'] = args[0]
        await message.answer('Ссылка на док установлена')
    else:
        CUR_PARAMS[peer_id]['doc'] = ''
        await message.answer('Ссылка на док удалена')


@BOT.on.message(MyRule(['game_info'], [0]))
async def cmd_game_info(message: Message, peer_id: int):
    game_link = f'https://{CUR_PARAMS[peer_id].get("cur_domain", "")}/GameDetails.aspx?gid={CUR_PARAMS[peer_id]["cur_json"]["GameId"]}'
    game_doc = CUR_PARAMS[peer_id].get('doc', 'Не установлен')
    await message.answer(f'Ссылка на игру: {game_link} \nСсылка на док: {game_doc} \n')


@BOT.on.message(MyRule(['*', 'geo'], [0, 2]))
async def cmd_geo(message: Message, args: list[str]):
    if args:
        await message.answer(message='', lat=args[0], long=args[1])
    else:
        await message.answer('Введите широту и долготу после команды через пробел')


@BOT.on.message(MyRule(['w', 'wf'], None))
async def cmd_w(message: Message, command: str, args: list[str], peer_id: int):
    full = True if command == 'wf' else False
    if peer_id in CUR_PARAMS:
        await send_screen(peer_id, f'https://ru.wikipedia.org/wiki/{' '.join(args)}', full=full)
    else:
        await message.answer('команда доступна только в авторизованном чате')


@BOT.on.message(MyRule(['buttons'], [0]))
async def cmd_buttons(message: Message):
    keyboard = (
        Keyboard(one_time=False, inline=False)
        .add(Text('/task'))
        .add(Text('/sectors'))
        .add(Text('/bonuses'))
        .row()
        .add(Text('/hint'))
        .add(Text('/screen'))
        .add(Text('/del_kb')))
    await message.answer(message='клавиатура добавлена', keyboard=keyboard.get_json())


@BOT.on.message(MyRule(['del_kb'], [0]))
async def cmd_del_kb(message: Message):
    await message.answer(message='клавиатура удалена', keyboard=EMPTY_KEYBOARD)


# Отправка ответов в движок
@BOT.on.message(text=['/<answer>'])
async def cmd_send_answer(message: Message, answer: str):
    # со ссылками сообщения отправляются дважды, нужно их фильтровать
    if message.attachments:
        return
    peer_id = message.peer_id
    from_id = message.from_id
    if not (CUR_PARAMS[peer_id]['accept_codes']):
        await message.answer('Прием кодов выключен! Для включения выполните /accept_codes')
        return

    sectors_list = []
    bonus_list = []

    if answer[0] == '!' and CUR_PARAMS[peer_id]['cur_json']['Level']['HasAnswerBlockRule']:
        answer = answer[1:]
        send_to_block = True
    else:
        send_to_block = False

    # Если блокировка, нет бонусов и ответ не с !:
    if (len(CUR_PARAMS[peer_id]["cur_json"]["Level"]["Bonuses"]) == 0) and CUR_PARAMS[peer_id]['cur_json']['Level']['HasAnswerBlockRule'] and not send_to_block:
        await message.answer('На уровне блокировка, в сектор вбивайте самостоятельно или через /!')
        return

    # По умолчанию вбивать в бонус при блокировке, если ответ без !
    if CUR_PARAMS[peer_id]['cur_json']['Level']['HasAnswerBlockRule'] and not send_to_block:
        answer_type = 'BonusAction'
        await message.answer('На уровне блокировка, вбиваю в бонус, в сектор вбивайте самостоятельно или через /!')
    else:
        answer_type = 'LevelAction'

    try:
        async with CUR_PARAMS[peer_id]["session"].get(f'https://{CUR_PARAMS[peer_id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[peer_id]["cur_json"]["GameId"]}?json=1') as response:
            response.raise_for_status()
            old_json = await response.json()
        answer_data = {'LevelId': CUR_PARAMS[peer_id]["cur_json"]['Level']['LevelId'], 'LevelNumber': CUR_PARAMS[peer_id]["cur_json"]['Level']['Number'], answer_type + '.answer': answer}

        async with CUR_PARAMS[peer_id]['session'].post(f'https://{CUR_PARAMS[peer_id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[peer_id]["cur_json"]["GameId"]}?json=1', data=answer_data) as response:
            response.raise_for_status()
            answer_json = await response.json()
    except Exception as e:
        await message.answer(f'Ошибка, возможно необходимо заново авторизоваться: {e}')
        logging.error(f"Ошибка работы бота, возможно необходимо заново авторизоваться: {e}", exc_info=True)
        return

    if answer_json['Event'] != 0:
        await check_engine(peer_id)
        return

    if answer_json['EngineAction'][answer_type]['IsCorrectAnswer']:
        if answer_type == 'LevelAction':
            for elem in answer_json['Level']['Sectors']:
                if elem['IsAnswered'] and elem["Answer"]["Answer"].lower() == answer.lower():
                    if elem in old_json['Level']['Sectors']:
                        sectors_list.append(f'⚪Баян! Сектор №{elem["Order"]} {elem["Name"] or ""}')
                    else:
                        sectors_list.append(f'🟢Сектор №{elem["Order"]} {elem["Name"] or ""} закрыт!')
                        CUR_PARAMS[peer_id]['sector_closers'][elem["SectorId"]] = from_id

        for elem in answer_json['Level']['Bonuses']:
            if elem['IsAnswered'] and elem["Answer"]["Answer"].lower() == answer.lower():
                if elem in old_json['Level']['Bonuses']:
                    bonus_list.append(
                        f'⚪Баян! Бонус №{elem["Number"]} {elem["Name"] or ""}\n{("Штрафное время: " if elem["Negative"] else "Бонусное время: ") + str(datetime.timedelta(seconds=elem["AwardTime"])) if elem["AwardTime"] != 0 else ""}\n{"Подсказка бонуса:" + chr(10) + elem["Help"] if elem["Help"] else ""}')
                else:
                    bonus_list.append(
                        f'Бонус №{elem["Number"]} {elem["Name"] or ""} закрыт\n{("🔴 Штрафное время: " if elem["Negative"] else "🟢 Бонусное время: ") + str(datetime.timedelta(seconds=elem["AwardTime"])) if elem["AwardTime"] != 0 else ""}\n{"Подсказка бонуса:" + chr(10) + elem["Help"] if elem["Help"] else ""}')
                    CUR_PARAMS[peer_id]['bonus_closers'][elem["BonusId"]] = from_id
        result_str = f'✅Ответ {answer} верный\n' + '\n'.join(sectors_list) + '\n' + '\n'.join(bonus_list)

        await message.answer(result_str)

    elif answer_json['EngineAction'][answer_type]['IsCorrectAnswer'] is False:
        await message.answer(f'❌Ответ {answer} неверный')
    elif answer_json['EngineAction'][answer_type]['IsCorrectAnswer'] is None:
        await message.answer(f'❓Ответа на код {answer} не было, возможно поле заблокировано')
    await check_engine(peer_id)


if __name__ == "__main__":
    try:
        BOT.run_forever()
    except KeyboardInterrupt:
        logging.info("Бот остановлен вручную.")
    except Exception as exc:
        logging.critical(f"Критическая ошибка работы бота: {exc}", exc_info=True)
