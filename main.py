from vkbot import VkBot
import logging
import datetime
import json
from time import sleep
import requests
import threading
from selenium import webdriver  # pip install selenium
from selenium.webdriver.firefox.options import Options
import re
import io
import base64
# from selenium.webdriver.support.ui import WebDriverWait
# from selenium.webdriver.support import expected_conditions as EC

import os.path
import configparser
import random

# Читаем конфиг
try:
    config = configparser.ConfigParser()
    config.read('settings.ini', encoding='utf-8')
    ADMIN_USERNAMES = tuple(config['Settings']['Admins'].split(','))  # Администраторы, которым разрешена авторизация бота в чате
    SECTORS_LEFT_ALERT = int(config['Settings']['Sectors_left_alert'])  # Количество оставшихся для закрытия секторов, с которого выводить оповещение, сколько осталось
    USER_AGENT = {'User-agent': 'Temig vk enbot'}  # Выставляемый в requests и selenium user-agent
    TASK_MAX_LEN = int(config['Settings']['Task_max_len'])  # Максимальное кол-во символов в одном сообщении, если превышает, то разбивается на несколько
    LANG = config['Settings']['Lang']
    CHECK_INTERVAL = int(config['Settings']['Check_interval'])
    TIMELEFT_ALERT1 = int(config['Settings']['Timeleft_alert1'])
    TIMELEFT_ALERT2 = int(config['Settings']['Timeleft_alert2'])
    VK_GROUP_ID = int(config['Settings']['Vk_group_id'])
    VK_TOKEN = config['Settings']['Vk_token']
    STOP_ACCEPT_CODES_WORDS = tuple(config['Settings']['Stop_accept_codes_words'].split(','))

except Exception as se:
    print(f'Error reading settings.ini config: {se}')

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# executable_dir = os.path.dirname(sys.executable)
folder_path = os.path.join(os.curdir, 'level_snapshots')
if not os.path.exists(folder_path):
    os.makedirs(folder_path)

if not VK_TOKEN or not VK_GROUP_ID:
    logging.error("Пожалуйста, установите переменные VK_TOKEN и VK_GROUP_ID в settings.ini")
    exit(1)

CUR_PARAMS = {}                 # словарь с текущими состояниями слежения в чатах

# Создаем экземпляр бота
BOT = VkBot(token=VK_TOKEN, group_id=VK_GROUP_ID)


@BOT.message_handler(commands=['help', 'start'])
def cmd_help(message):
    BOT.send_message(message['peer_id'], r'''Temig vk enbot v1.0
https://github.com/temig74
/help - этот help
/auth домен id_игры логин пароль [id_чата] - авторизовать бота на игру в игровом чате (или в личке, добавив id_чата)
/stop_auth - отключить чат
/get_id - получить id чата и пользователя
/game_monitor [0] - включить/[отключить] слежение за игрой
/sector, /сектор [level№] - показать сектора [прошедшего_уровня]
/sectors_left - оставшиеся сектора на уровне
/bonus, /бонус [level№] - показать бонусы [прошедшего_уровня]
/hint, /хинт - показать подсказки
/task, /таск - показать текущее задание
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
/set_players @игрок1 @игрок2 - установить список полевых игроков
/open_browser открыть бразуер на компьютере, где запущен бот, привязанный к сессии бота (необходим firefox)
/game_info - информация об игре
/set_doc - установить ссылку на гуглдок
/buttons - добавить клавиатуру с кнопками
/w название_статьи - скрин статьи из вики
/wf название_статьи - полный скрин статьи из вики
''')


@BOT.message_handler(commands=['auth'])
def cmd_auth(message):
    # VK повторно отправляет сообщение, вставляя ссылку как attachments, второе сообщение не обрабатываем
    if message['attachments']:
        return

    if str(message['from_id']) not in ADMIN_USERNAMES:
        BOT.send_message(message['peer_id'], 'Недостаточно прав для авторизации бота')
        return

    input_list = message['text'].split()[1:]

    if len(input_list) > 5 or len(input_list) < 4:
        BOT.send_message(message['peer_id'], 'Недостаточно аргументов, введите команду в формате /auth домен id_игры логин пароль [id_чата]')
        return

    if len(input_list) == 5 and input_list[4].isdigit():
        cur_chat_id = int(input_list[4])
    elif len(input_list) == 4:
        cur_chat_id = message['peer_id']
    else:
        BOT.send_message(message['peer_id'], 'Неверный формат id чата')
        return

    if not input_list[1].isdigit():
        BOT.send_message(message['peer_id'], 'Неверный формат id игры')
        return

    my_domain = input_list[0]
    my_game_id = input_list[1]
    my_login = input_list[2]
    my_password = input_list[3]
    my_session = requests.session()
    my_session.headers.update(USER_AGENT)

    try:
        auth_request_json = my_session.post(f'https://{my_domain}/login/signin?json=1', data={'Login': my_login, 'Password': my_password}).json()
    except Exception as e:
        BOT.send_message(message['peer_id'], f'Ошибка запроса авторизации, возможно неверно указан домен:{e}')
        logging.error(f"Ошибка авторизации бота: {e}", exc_info=True)
        return

    match auth_request_json['Error']:
        case 1:
            BOT.send_message(message['peer_id'], 'Превышено количество неправильных  попыток авторизации')
            return
        case 2:
            BOT.send_message(message['peer_id'], 'Неверный логин или пароль')
            return
        case 3:
            BOT.send_message(message['peer_id'], 'Пользователь или в Cибири, или в черном списке, или на домене нельзя авторизовываться с других доменов')
            return
        case 4:
            BOT.send_message(message['peer_id'], 'Блокировка по IP')
            return
        case 5:
            BOT.send_message(message['peer_id'], 'В процессе авторизации произошла ошибка на сервере')
            return
        case 6:
            BOT.send_message(message['peer_id'], 'Ошибка')
            return
        case 7:
            BOT.send_message(message['peer_id'], 'Пользователь заблокирован администратором')
            return
        case 8:
            BOT.send_message(message['peer_id'], 'Новый пользователь не активирован')
            return
        case 9:
            BOT.send_message(message['peer_id'], 'Действия пользователя расценены как брутфорс')
            return
        case 10:
            BOT.send_message(message['peer_id'], 'Пользователь не подтвердил e-mail')
            return
        case 0:
            print('Авторизация успешна')
            try:
                # Получаем информацию об игре
                cur_json = my_session.get(f'https://{my_domain}/GameEngines/Encounter/Play/{my_game_id}?json=1').json()
            except Exception as e:
                BOT.send_message(message['peer_id'], f'Ошибка запроса авторизации, возможно неверно указан id игры: {e}')
                logging.error(f"Ошибка авторизации бота: {e}", exc_info=True)
                return

            BOT.send_message(message['peer_id'], 'Авторизация успешна')  # Только если успешна, то заново инициализируем словарь параметров чата
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
            # запускаем firefox браузер, который будем использовать для скриншотов уровня
            options = Options()
            options.add_argument("--headless")  # не отображаемый в системе
            options.set_preference("general.useragent.override", USER_AGENT['User-agent'])
            my_driver = webdriver.Firefox(options=options)
            # my_driver.get(f'https://{my_domain}')
            my_driver.get(f'https://{my_domain}/GameEngines/Encounter/Play/{my_game_id}')
            # my_driver.add_cookie({'name': 'atoken', 'value': my_session.cookies.get_dict()['atoken'], 'domain': '.en.cx', 'secure': False, 'httpOnly': True, 'session': True})
            my_driver.add_cookie({'name': 'atoken', 'value': my_session.cookies.get_dict()['atoken'], 'domain': '.' + my_domain, 'secure': False, 'httpOnly': True, 'session': True})
            my_driver.add_cookie({'name': 'stoken', 'value': my_session.cookies.get_dict()['stoken'], 'domain': '.' + my_domain, 'secure': False, 'httpOnly': False, 'session': True})
            CUR_PARAMS[cur_chat_id]['driver'] = my_driver
            BOT.send_message(message['peer_id'], 'Виртуальный браузер запущен')


def send_screen(peer_id, link, full=False):
    if CUR_PARAMS[peer_id]['driver']:
        CUR_PARAMS[peer_id]['driver'].get(link)
        # BOT.send_photo_from_base64(CUR_PARAMS[peer_id]['driver'].get_full_page_screenshot_as_base64(), peer_id, '') #  хуже качество
        if full:
            BOT.send_stringio_file(peer_id, io.BytesIO(base64.b64decode(CUR_PARAMS[peer_id]['driver'].get_full_page_screenshot_as_base64())), 'screen_file.png')  # файлом лучше качество, если большой скрин
        else:
            BOT.send_stringio_file(peer_id, io.BytesIO(base64.b64decode(CUR_PARAMS[peer_id]['driver'].get_screenshot_as_base64())), 'screen_file.png')  # файлом лучше качество, если большой скрин
    else:
        BOT.send_message(peer_id, 'Виртуальный браузер не запущен')


@BOT.message_handler(commands=['screen', 'скрин', 'fscreen', 'фскрин'])
def cmd_screen(message):
    full = message['text'].split()[0] in ['/fscreen', '/фскрин']
    send_screen(message['peer_id'], f'https://{CUR_PARAMS[message['peer_id']]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message['peer_id']]["cur_json"]["GameId"]}?lang={LANG}', full)


# Отправить информацию о текущем уровне
def send_curlevel_info(cur_chat, cur_json):
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
    BOT.send_message(cur_chat, gameinfo_str, CUR_PARAMS[cur_chat]['parser'])

    # Отдельно выводим задание
    if len(cur_json['Level']['Tasks']) > 0:
        # gamelevel_str = cur_json['Level']['Tasks'][0]['TaskText']
        gamelevel_str = cur_json['Level']['Tasks'][0]['TaskText']
    else:
        gamelevel_str = 'Нет заданий на уровне'

    # Если очень большой текст на уровне, то сплит
    for i in range(0, len(gamelevel_str), TASK_MAX_LEN):
        BOT.send_message(cur_chat, gamelevel_str[i:i + TASK_MAX_LEN], CUR_PARAMS[cur_chat]['parser'])


def check_engine(cur_chat_id):
    try:
        game_json = CUR_PARAMS[cur_chat_id]["session"].get(f'https://{CUR_PARAMS[cur_chat_id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[cur_chat_id]["cur_json"]["GameId"]}?json=1&lang={LANG}').json()
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as CE:
        print(f'Ошибка соединения {CE}, переподключаюсь')
        return True

    except Exception as e:
        BOT.send_message(cur_chat_id, f'Ошибка мониторинга, возможно необходимо заново авторизоваться: {e}')
        logging.error(f"Ошибка мониторинга бота: {e}", exc_info=True)
        return

    # False - если цикл надо прервать (Серьезная ошибка), True - если продолжать
    match game_json['Event']:
        case 2:
            BOT.send_message(cur_chat_id, 'Игра с указанным id не существует')
            return
        case 4:
            BOT.send_message(cur_chat_id, 'Ошибка авторизации')
            return
        case 5:
            print("Game hasn't started yet, continue monitoring")
            return True  # игра еще не началась, продолжаем мониторить
        case 6 | 17:
            BOT.send_message(cur_chat_id, 'Игра закончилась')
            CUR_PARAMS[cur_chat_id]['monitoring_flag'] = False
            sleep(7)
            BOT.send_message(cur_chat_id, 'Авторизация чата отключена')
            CUR_PARAMS.pop(cur_chat_id, None)  # Освобождаем в памяти словарь чата
            return
        case 7 | 8:
            BOT.send_message(cur_chat_id, 'Заявка не подана')
            return
        case 9:
            BOT.send_message(cur_chat_id, 'Команда не принята в игру')
            return
        case 10:
            BOT.send_message(cur_chat_id, 'Аккаунт не в команде')
            return
        case 11:
            BOT.send_message(cur_chat_id, 'Аккаунт не активен в команде')
            return
        case 12:
            BOT.send_message(cur_chat_id, 'Игра не содержит уровней')
            return
        case 13:
            BOT.send_message(cur_chat_id, 'Превышено количество участников')
            return
        case 16 | 18 | 21:
            BOT.send_message(cur_chat_id, 'Уровень был снят')
            check_engine(cur_chat_id)
            return True
        case 19 | 22:
            BOT.send_message(cur_chat_id, 'Уровень пройден по автопереходу')
            check_engine(cur_chat_id)
            return True
        case 20:
            check_engine(cur_chat_id)
            return True  # все секторы выполнены
        case 0:
            old_json = CUR_PARAMS[cur_chat_id]['cur_json']  # предыдущий json
            CUR_PARAMS[cur_chat_id]['cur_json'] = game_json  # текущий json

            # Игра началась
            if old_json['Level'] is None:
                BOT.send_message(cur_chat_id, 'Игра началась!\n')
                send_curlevel_info(cur_chat_id, game_json)
                return True

            # Проверка, что поменялся номер уровня, т.е. произошел АП
            if old_json['Level']['Number'] != game_json['Level']['Number']:
                CUR_PARAMS[cur_chat_id]['5_min_sent'] = False
                CUR_PARAMS[cur_chat_id]['1_min_sent'] = False
                BOT.send_message(cur_chat_id, 'АП!\n' + ' '.join(CUR_PARAMS[cur_chat_id].get('players', '')))
                if CUR_PARAMS[cur_chat_id]['send_screen']:
                    send_screen(cur_chat_id, f'https://{CUR_PARAMS[cur_chat_id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[cur_chat_id]["cur_json"]["GameId"]}?lang={LANG}', full=True)

                # отключение ввода кодов при обнаружении штрафных
                if len(game_json['Level']['Tasks']) > 0:
                    if any(item in game_json['Level']['Tasks'][0]['TaskText'].lower() for item in STOP_ACCEPT_CODES_WORDS):
                        CUR_PARAMS[cur_chat_id]['accept_codes'] = False
                        BOT.send_message(cur_chat_id, 'В тексте обнаружена информация о штрафах или ложных кодах, ввод кодов отключен! Для включения выполните /accept_codes')

                send_curlevel_info(cur_chat_id, game_json)

                if len(game_json['Level']['Tasks']) > 0:
                    send_kml_info(cur_chat_id, game_json['Level']['Tasks'][0]['TaskText'], game_json['Level']['Number'])

                # Сохраняем информацию о пройденном уровне
                CUR_PARAMS[cur_chat_id]['OLD_LEVELS'][str(old_json['Level']['Number'])] = {}
                CUR_PARAMS[cur_chat_id]['OLD_LEVELS'][str(old_json['Level']['Number'])]['Event'] = old_json['Event']
                CUR_PARAMS[cur_chat_id]['OLD_LEVELS'][str(old_json['Level']['Number'])]['Level'] = old_json['Level']

                # Запись в файл
                json_file_data = CUR_PARAMS[cur_chat_id]['OLD_LEVELS']
                json_filename = f'{cur_chat_id}.{CUR_PARAMS[cur_chat_id]["cur_json"]["GameId"]}'
                if os.path.isfile('level_snapshots/'+json_filename):
                    with open('level_snapshots/'+json_filename) as json_file:
                        json_file_data.update(json.load(json_file))
                with open('level_snapshots/'+json_filename, 'w') as json_file:
                    json.dump(json_file_data, json_file)
                return True

            # проверка на изменение текста уровня
            if old_json['Level']['Tasks'] != game_json['Level']['Tasks']:
                BOT.send_message(cur_chat_id, 'Задание уровня изменилось')

            # проверка на сообщения на уровне:
            for elem in game_json['Level']['Messages']:
                if elem not in old_json['Level']['Messages']:
                    BOT.send_message(cur_chat_id, f'Добавлено сообщение: {elem["MessageText"]}', CUR_PARAMS[cur_chat_id]['parser'])

            # проверка на количество секторов на уровне:
            if len(old_json['Level']['Sectors']) != len(game_json['Level']['Sectors']):
                BOT.send_message(cur_chat_id, 'Количество секторов на уровне изменилось')

            # проверка на количество бонусов на уровне:
            if len(old_json['Level']['Bonuses']) != len(game_json['Level']['Bonuses']):
                BOT.send_message(cur_chat_id, 'Количество бонусов на уровне изменилось')

            # проверка на количество необходимых секторов:
            if old_json['Level']['RequiredSectorsCount'] != game_json['Level']['RequiredSectorsCount']:
                BOT.send_message(cur_chat_id, 'Количество необходимых для прохождения секторов изменилось')

            # проверка на кол-во оставшихся секторов:
            cur_sectors_left = game_json['Level']['SectorsLeftToClose']
            if old_json['Level']['SectorsLeftToClose'] != cur_sectors_left and cur_sectors_left <= SECTORS_LEFT_ALERT:
                sector_list = [str(elem['Name']) for elem in game_json['Level']['Sectors'] if not (elem['IsAnswered'])]
                BOT.send_message(cur_chat_id, f'Осталось секторов: [{cur_sectors_left}]. Оставшиеся: {", ".join(sector_list)}')

            # Проверка, что пришла подсказка
            if len(CUR_PARAMS[cur_chat_id]["cur_json"]['Level']['Helps']) != len(old_json['Level']['Helps']):
                BOT.send_message(cur_chat_id, 'Была добавлена подсказка')
            else:
                for i, elem in enumerate(CUR_PARAMS[cur_chat_id]["cur_json"]['Level']['Helps']):
                    if elem['HelpText'] != old_json['Level']['Helps'][i]['HelpText']:
                        # BOT.send_message(cur_chat_id, f'Подсказка {i + 1}: {elem["HelpText"]}')
                        BOT.send_message(cur_chat_id, f'Подсказка {i + 1}: {elem["HelpText"]}', CUR_PARAMS[cur_chat_id]['parser'])
                        send_kml_info(cur_chat_id, elem["HelpText"], f'{CUR_PARAMS[cur_chat_id]["cur_json"]["Level"]["Number"]}_{i+1}')

            # мониторинг закрытия секторов
            if CUR_PARAMS[cur_chat_id]['sector_monitor']:
                sector_msg = ''
                for elem in game_json['Level']['Sectors']:
                    if elem not in old_json['Level']['Sectors'] and elem["IsAnswered"] and (elem['SectorId'] not in CUR_PARAMS[cur_chat_id]['sector_closers']):
                        sector_msg += f'✅№{elem["Order"]} {elem["Name"]} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]})\n'
                if sector_msg != '':
                    BOT.send_message(cur_chat_id, sector_msg)

            # мониторинг закрытия бонусов
            if CUR_PARAMS[cur_chat_id]['bonus_monitor']:
                for elem in game_json['Level']['Bonuses']:
                    if elem not in old_json['Level']['Bonuses'] and elem["IsAnswered"] and (elem['BonusId'] not in CUR_PARAMS[cur_chat_id]['sector_closers']):
                        BOT.send_message(cur_chat_id, f'{"🔴" if elem["Negative"] else "🟢"} №{elem["Number"]} {elem["Name"] or ""} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]}) {"Штраф: " if elem["Negative"] else "Бонус: "} {datetime.timedelta(seconds=elem["AwardTime"])}\n{"Подсказка бонуса:" + chr(10) + elem["Help"] if elem["Help"] else ""}', CUR_PARAMS[cur_chat_id]['parser'])

                        if elem["Help"]:
                            send_kml_info(cur_chat_id, elem["Help"], CUR_PARAMS[cur_chat_id]["cur_json"]["Level"]["Number"])

            # мониторинг времени до автоперехода
            if TIMELEFT_ALERT1 > game_json['Level']['TimeoutSecondsRemain'] > 0 and not (CUR_PARAMS[cur_chat_id]['5_min_sent']):
                BOT.send_message(cur_chat_id, 'До автоперехода осталось менее 5 минут!')
                CUR_PARAMS[cur_chat_id]['5_min_sent'] = True
            if TIMELEFT_ALERT2 > game_json['Level']['TimeoutSecondsRemain'] > 0 and not (CUR_PARAMS[cur_chat_id]['1_min_sent']):
                BOT.send_message(cur_chat_id, 'До автоперехода осталось менее 1 минуты!')
                CUR_PARAMS[cur_chat_id]['1_min_sent'] = True
    return True


@BOT.send_answer()
def send_answer(message):
    if message['text'][0] != '/':
        return
    if not (CUR_PARAMS[message['peer_id']]['accept_codes']):
        BOT.send_message(message['peer_id'], 'Прием кодов выключен! Для включения выполните /accept_codes')
        return
    sectors_list = []
    bonus_list = []

    if message['text'][1] == '!' and CUR_PARAMS[message['peer_id']]['cur_json']['Level']['HasAnswerBlockRule']:
        answer = message['text'][2:]
        send_to_block = True
    else:
        answer = message['text'][1:]
        send_to_block = False

    # Если блокировка, нет бонусов и ответ не с !:
    if (len(CUR_PARAMS[message['peer_id']]["cur_json"]["Level"]["Bonuses"]) == 0) and CUR_PARAMS[message['peer_id']]['cur_json']['Level']['HasAnswerBlockRule'] and not send_to_block:
        BOT.send_message(message['peer_id'], 'На уровне блокировка, в сектор вбивайте самостоятельно или через /!')
        return

    # По умолчанию вбивать в бонус при блокировке, если ответ без !
    if CUR_PARAMS[message['peer_id']]['cur_json']['Level']['HasAnswerBlockRule'] and not send_to_block:
        answer_type = 'BonusAction'
        BOT.send_message(message['peer_id'], 'На уровне блокировка, вбиваю в бонус, в сектор вбивайте самостоятельно или через /!')
    else:
        answer_type = 'LevelAction'

    try:
        old_json = CUR_PARAMS[message['peer_id']]["session"].get(f'https://{CUR_PARAMS[message['peer_id']]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message['peer_id']]["cur_json"]["GameId"]}?json=1').json()
        answer_json = CUR_PARAMS[message['peer_id']]['session'].post(f'https://{CUR_PARAMS[message['peer_id']]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message['peer_id']]["cur_json"]["GameId"]}?json=1', data={
            'LevelId': CUR_PARAMS[message['peer_id']]["cur_json"]['Level']['LevelId'],
            'LevelNumber': CUR_PARAMS[message['peer_id']]["cur_json"]['Level']['Number'],
            answer_type + '.answer': answer}).json()
    except Exception as e:
        BOT.send_message(message['peer_id'], f'Ошибка, возможно необходимо заново авторизоваться: {e}')
        logging.error(f"Ошибка работы бота, возможно необходимо заново авторизоваться: {e}", exc_info=True)
        return

    if answer_json['Event'] != 0:
        check_engine(message['peer_id'])
        return

    if answer_json['EngineAction'][answer_type]['IsCorrectAnswer']:
        if answer_type == 'LevelAction':
            for elem in answer_json['Level']['Sectors']:
                if elem['IsAnswered'] and elem["Answer"]["Answer"].lower() == answer.lower():
                    if elem in old_json['Level']['Sectors']:
                        sectors_list.append(f'⚪Баян! Сектор №{elem["Order"]} {elem["Name"] or ""}')
                    else:
                        sectors_list.append(f'🟢Сектор №{elem["Order"]} {elem["Name"] or ""} закрыт!')
                        CUR_PARAMS[message['peer_id']]['sector_closers'][elem["SectorId"]] = message['from_id']

        for elem in answer_json['Level']['Bonuses']:
            if elem['IsAnswered'] and elem["Answer"]["Answer"].lower() == answer.lower():
                if elem in old_json['Level']['Bonuses']:
                    bonus_list.append(
                        f'⚪Баян! Бонус №{elem["Number"]} {elem["Name"] or ""}\n{("Штрафное время: " if elem["Negative"] else "Бонусное время: ") + str(datetime.timedelta(seconds=elem["AwardTime"])) if elem["AwardTime"] != 0 else ""}\n{"Подсказка бонуса:" + chr(10) + elem["Help"] if elem["Help"] else ""}')
                else:
                    bonus_list.append(
                        f'Бонус №{elem["Number"]} {elem["Name"] or ""} закрыт\n{("🔴 Штрафное время: " if elem["Negative"] else "🟢 Бонусное время: ") + str(datetime.timedelta(seconds=elem["AwardTime"])) if elem["AwardTime"] != 0 else ""}\n{"Подсказка бонуса:" + chr(10) + elem["Help"] if elem["Help"] else ""}')
                    CUR_PARAMS[message['peer_id']]['bonus_closers'][elem["BonusId"]] = message['from_id']
        result_str = f'✅Ответ {answer} верный\n' + '\n'.join(sectors_list) + '\n' + '\n'.join(bonus_list)

        BOT.send_message(message['peer_id'], result_str)

    elif answer_json['EngineAction'][answer_type]['IsCorrectAnswer'] is False:
        BOT.send_message(message['peer_id'], f'❌Ответ {answer} неверный')
    elif answer_json['EngineAction'][answer_type]['IsCorrectAnswer'] is None:
        BOT.send_message(message['peer_id'], f'❓Ответа на код {answer} не было, возможно поле заблокировано')
    check_engine(message['peer_id'])


def monitoring_func(cur_chat_id):
    start_time = datetime.datetime.now()
    BOT.send_message(cur_chat_id, 'Мониторинг включен')
    while CUR_PARAMS[cur_chat_id]['monitoring_flag']:
        print(f'Слежение за игрой в чате {cur_chat_id} работает {datetime.datetime.now()-start_time}')
        sleep(CHECK_INTERVAL+random.uniform(-1, 1))
        try:
            if not (check_engine(cur_chat_id)):
                break
        except Exception as e:
            logging.error(f"Ошибка функции check_engine, продолжаю мониторинг: {e}", exc_info=True)
    CUR_PARAMS[cur_chat_id]['monitoring_flag'] = False
    BOT.send_message(cur_chat_id, 'Мониторинг выключен')


@BOT.message_handler(commands=['game_monitor'])
def cmd_game_monitor(message):
    if len(message['text'].split()) > 1 and message['text'].split()[1] == '0':
        CUR_PARAMS[message['peer_id']]['monitoring_flag'] = False
        sleep(7)
    else:
        if not (CUR_PARAMS[message['peer_id']]['monitoring_flag']):
            CUR_PARAMS[message['peer_id']]['monitoring_flag'] = True
            # threading.Thread(target=monitoring_func(message['peer_id'])).start()
            thread = threading.Thread(target=monitoring_func, args=(message['peer_id'],))
            thread.start()
        else:
            BOT.send_message(message['peer_id'], 'Слежение уже запущено')


@BOT.message_handler(commands=['stop_auth'])
def cmd_stop_auth(message):
    if str(message['from_id']) not in ADMIN_USERNAMES:
        BOT.send_message(message['peer_id'], 'Недостаточно прав для отключения авторизации бота')
        return

    CUR_PARAMS[message['peer_id']]['monitoring_flag'] = False
    BOT.send_message(message['peer_id'], 'Авторизация чата отключена')
    sleep(7)
    CUR_PARAMS.pop(message['peer_id'], None)  # Освобождаем в памяти словарь чата


@BOT.message_handler(commands=['get_id'])
def cmd_get_id(message):
    BOT.send_message(message['peer_id'], str(f'id чата: {message['peer_id']}\nid пользователя: {message['from_id']}'))


@BOT.message_handler(commands=['hint', 'хинт'])
def cmd_hint(message):
    result_str = ''
    try:
        game_json = CUR_PARAMS[message['peer_id']]['session'].get(f'https://{CUR_PARAMS[message['peer_id']]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message['peer_id']]["cur_json"]["GameId"]}?json=1').json()
    except Exception as e:
        BOT.send_message(message['peer_id'], f'Ошибка, возможно необходимо заново авторизоваться: {e}')
        logging.error(f"Ошибка, возможно необходимо заново авторизоваться: {e}", exc_info=True)
        return

    if game_json['Event'] != 0:
        BOT.send_message(message['peer_id'], 'Ошибка')
        return

    for elem in game_json['Level']['Helps']:
        if elem['RemainSeconds'] == 0:
            result_str += f'Подсказка {elem["Number"]}:\n{elem["HelpText"]}\n{"_"*30}\n\n'
        else:
            result_str += f'Подсказка {elem["Number"]}: Будет через {datetime.timedelta(seconds=elem["RemainSeconds"])}\n{"_"*30}\n\n'
    if result_str == '':
        result_str = 'Нет подсказок'
    BOT.send_message(message['peer_id'], result_str, CUR_PARAMS[message['peer_id']]['parser'])


@BOT.message_handler(commands=['task', 'таск'])
def cmd_task(message):
    check_engine(message['peer_id'])
    send_curlevel_info(message['peer_id'], CUR_PARAMS[message['peer_id']]['cur_json'])
    cmd_hint(message)


@BOT.message_handler(commands=['open_browser'])
def cmd_open_browser(message):
    if str(message['from_id']) not in ADMIN_USERNAMES:
        BOT.send_message(message['peer_id'], 'Недостаточно прав для запуска браузера')
        return

    my_options = Options()
    my_options.set_preference("general.useragent.override", USER_AGENT['User-agent'])
    my_driver = webdriver.Firefox(options=my_options)
    # my_driver.get(f'https://{CUR_PARAMS[message['peer_id']]["cur_domain"]}')
    my_driver.get(f'https://{CUR_PARAMS[message['peer_id']]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message['peer_id']]["cur_json"]["GameId"]}')
    # my_driver.add_cookie({'name': 'atoken', 'value': CUR_PARAMS[message['peer_id']]['session'].cookies.get_dict()['atoken'], 'domain': '.en.cx', 'secure': False, 'httpOnly': True, 'session': True})
    my_driver.add_cookie({'name': 'atoken', 'value': CUR_PARAMS[message['peer_id']]['session'].cookies.get_dict()['atoken'], 'domain': '.' + CUR_PARAMS[message['peer_id']]['cur_domain'], 'secure': False, 'httpOnly': True, 'session': True})
    my_driver.add_cookie({'name': 'stoken', 'value': CUR_PARAMS[message['peer_id']]['session'].cookies.get_dict()['stoken'], 'domain': '.' + CUR_PARAMS[message['peer_id']]['cur_domain'], 'secure': False, 'httpOnly': False, 'session': True})
    my_driver.get(f'https://{CUR_PARAMS[message['peer_id']]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message['peer_id']]["cur_json"]["GameId"]}')


@BOT.message_handler(commands=['time'])
def cmd_time(message):
    try:
        game_json = CUR_PARAMS[message['peer_id']]['session'].get(f'https://{CUR_PARAMS[message['peer_id']]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message['peer_id']]["cur_json"]["GameId"]}?json=1').json()
    except Exception as e:
        BOT.send_message(message['peer_id'], f'Ошибка, возможно необходимо заново авторизоваться {e}')
        logging.error(f"Ошибка, возможно необходимо заново авторизоваться: {e}", exc_info=True)
        return

    if game_json['Event'] != 0:
        BOT.send_message(message['peer_id'], 'Ошибка')
        return
    if game_json["Level"]["Timeout"] == 0:
        BOT.send_message(message['peer_id'], f'Автопереход отсутствует')
        return
    BOT.send_message(message['peer_id'], f'Автопереход через {datetime.timedelta(seconds=game_json["Level"]["TimeoutSecondsRemain"])}')


@BOT.message_handler(commands=['sector', 'sectors', 'сектор', 'секторы', 'sectors_left'])
def cmd_sectors(message):
    # Для работы кнопок, там идет сначала обращение к боту
    if message['text'].startswith('['):
        text = message['text'].split(maxsplit=1)[1]
    else:
        text = message['text']

    # Если указан номер уровня, то загружаем из OLD_LEVELS
    cmd = text[1:].split()[0].lower()
    if len(text.split()) == 2:
        if text.split()[1] in CUR_PARAMS[message['peer_id']]['OLD_LEVELS']:
            game_json = CUR_PARAMS[message['peer_id']]['OLD_LEVELS'][message['text'].split()[1]]
        else:
            BOT.send_message(message['peer_id'], 'Уровень не найден в прошедших')
            return
    else:
        try:
            game_json = CUR_PARAMS[message['peer_id']]['session'].get(f'https://{CUR_PARAMS[message['peer_id']]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message['peer_id']]["cur_json"]["GameId"]}?json=1').json()
        except Exception as e:
            BOT.send_message(message['peer_id'], f'Ошибка, возможно необходимо заново авторизоваться: {e}')
            logging.error(f"Ошибка, возможно необходимо заново авторизоваться: {e}", exc_info=True)
            return

    result_str = ''

    if game_json['Event'] != 0:
        BOT.send_message(message['peer_id'], 'Ошибка')
        return

    for elem in game_json['Level']['Sectors']:
        if elem['IsAnswered']:
            if cmd in ('sector', 'сектор', 'секторы', 'sectors'):
                result_str += f'✅№{elem["Order"]} {elem["Name"]} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]}) {CUR_PARAMS[message['peer_id']]["sector_closers"].get(elem["SectorId"], "")}\n'
        else:
            result_str += f'❌№{elem["Order"]} {elem["Name"]}\n'
    if result_str == '':
        result_str = 'Нет секторов'

    result_str = f'Осталось закрыть: {game_json["Level"]["SectorsLeftToClose"] if game_json["Level"]["SectorsLeftToClose"] > 0 else 1} из {len(game_json["Level"]["Sectors"]) if len(game_json["Level"]["Sectors"]) > 0 else 1}\n' + result_str

    for i in range(0, len(result_str), TASK_MAX_LEN):
        BOT.send_message(message['peer_id'], result_str[i:i + TASK_MAX_LEN])


@BOT.message_handler(commands=['bonus', 'bonuses', 'бонус'])
def cmd_bonuses(message):
    if message['text'].startswith('['):
        text = message['text'].split(maxsplit=1)[1]
    else:
        text = message['text']

    if len(text.split()) == 2:
        if text.split()[1] in CUR_PARAMS[message['peer_id']]['OLD_LEVELS']:
            game_json = CUR_PARAMS[message['peer_id']]['OLD_LEVELS'][message['text'].split()[1]]
        else:
            BOT.send_message(message['peer_id'], 'Уровень не найден в прошедших')
            return
    else:
        try:
            game_json = CUR_PARAMS[message['peer_id']]['session'].get(f'https://{CUR_PARAMS[message['peer_id']]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message['peer_id']]["cur_json"]["GameId"]}?json=1').json()
        except Exception as e:
            BOT.send_message(message['peer_id'], f'Ошибка, возможно необходимо заново авторизоваться: {e}')
            logging.error(f"Ошибка, возможно необходимо заново авторизоваться: {e}", exc_info=True)
            return

    result_str = ''

    if game_json['Event'] != 0:
        BOT.send_message(message['peer_id'], 'Ошибка')
        return

    for elem in game_json['Level']['Bonuses']:
        if elem['IsAnswered']:
            result_str += f'{"🔴" if elem["Negative"] else "🟢"}№{elem["Number"]} {elem["Name"] or ""} {elem["Help"] or ""} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]}) {CUR_PARAMS[message['peer_id']]["bonus_closers"].get(elem["BonusId"], "")} {"Штраф: " if elem["Negative"] else "Бонус: "} {datetime.timedelta(seconds=elem["AwardTime"])}\n'
        else:
            result_str += f'{"✖Истёк" if elem["Expired"] else "❌"}№{elem["Number"]} {elem["Name"] or ""} {elem["Task"] or ""} {"Будет доступен через "+str(datetime.timedelta(seconds=elem["SecondsToStart"])) if elem["SecondsToStart"] != 0 else ""} {"Осталось на выполнение: "+str(datetime.timedelta(seconds=elem["SecondsLeft"])) if elem["SecondsLeft"] != 0 else ""}\n'
    if result_str == '':
        result_str = 'Нет бонусов'

    for i in range(0, len(result_str), TASK_MAX_LEN):
        BOT.send_message(message['peer_id'], result_str[i:i + TASK_MAX_LEN], CUR_PARAMS[message['peer_id']]['parser'])


@BOT.message_handler(commands=['load_old_json'])
def cmd_load_old_json(message):
    json_filename = str(message['peer_id']) + '.' + str(CUR_PARAMS[message['peer_id']]["cur_json"]["GameId"])
    if os.path.isfile('level_snapshots/'+json_filename):
        with open('level_snapshots/'+json_filename, 'r') as json_file:
            CUR_PARAMS[message['peer_id']]['OLD_LEVELS'].update(json.load(json_file))
    else:
        BOT.send_message(message['peer_id'], 'Файл не существует')


@BOT.message_handler(commands=['accept_codes', 'sector_monitor', 'bonus_monitor', 'route_builder', 'send_screen', 'parser'])
def switch_flag(message):
    d = {'accept_codes': 'Прием кодов',
         'sector_monitor': 'Мониторинг секторов',
         'bonus_monitor': 'Мониторинг бонусов',
         'route_builder': 'Построитель маршрутов',
         'send_screen': 'Отправитель скринов',
         'parser': 'Парсер HTML'
         }
    cmd = message['text'][1:].split()[0].split('@')[0].lower()
    if len(message['text'].split()) == 2 and message['text'].split()[1] == '0':
        cmd_flag = False
    else:
        cmd_flag = True
    CUR_PARAMS[message['peer_id']][cmd] = cmd_flag
    BOT.send_message(message['peer_id'], f'{d.get(cmd)} {"включен" if cmd_flag else "выключен"}')


# список игроков для тегания например при АПе уровня
@BOT.message_handler(commands=['set_players'])
def cmd_set_players(message):
    cmd, *args = message['text'].split()
    CUR_PARAMS[message['peer_id']]['players'] = args
    BOT.send_message(message['peer_id'], 'Список игроков установлен')


@BOT.message_handler(commands=['set_doc'])
def cmd_set_doc(message):
    if message['attachments']:
        return
    doc_link = message['text'].split()[1]
    CUR_PARAMS[message['peer_id']]['doc'] = doc_link
    BOT.send_message(message['peer_id'], 'Ссылка на док установлена')


@BOT.message_handler(commands=['game_info'])
def cmd_game_info(message):
    game_link = f'https://{CUR_PARAMS[message['peer_id']].get("cur_domain", "")}/GameDetails.aspx?gid={CUR_PARAMS[message['peer_id']]["cur_json"]["GameId"]}'
    game_doc = CUR_PARAMS[message['peer_id']].get('doc', 'Не установлен')
    BOT.send_message(message['peer_id'], f'Ссылка на игру: {game_link} \nСсылка на док: {game_doc} \n')


@BOT.message_handler(commands=['*', 'geo'])
def cmd_geo(message):
    input_lst = message['text'].replace(',', ' ').split()
    if len(input_lst) == 3:
        BOT.send_location(message['peer_id'], '', input_lst[1], input_lst[2])


def gen_kml2(text: str):
    coord_list = re.findall(r'-?\d{1,2}\.\d{3,10}[, ]*-?\d{1,3}\.\d{3,10}', text)
    if not coord_list:
        return
    result_list = []
    kml = '<kml><Document>'
    for cnt, elem in enumerate(coord_list):
        c = re.findall(r'-?\d{1,3}\.\d{3,10}', elem)
        new_point = f'<Point><coordinates>{c[1]},{c[0]},0.0</coordinates></Point>'
        if new_point not in kml:
            kml += f'<Placemark><name>Point {cnt+1}</name>{new_point}</Placemark>'
            result_list.append((c[0], c[1]))
    kml += '</Document></kml>'
    buf_file = io.StringIO()
    buf_file.write(kml)
    buf_file.seek(0, 0)
    return buf_file, result_list  # Возвращаем кортеж из файла kml и списка координат


def send_kml_info(cur_chat, parse_text, level_num):
    kml_var = gen_kml2(parse_text)
    if kml_var:
        kml_var[0].name = f'points{level_num}.kml'
        BOT.send_stringio_file(cur_chat, kml_var[0], kml_var[0].name)
        BOT.send_location(cur_chat, f'{kml_var[1][0][0]}, {kml_var[1][0][1]}', kml_var[1][0][0], kml_var[1][0][1])


@BOT.message_handler(commands=['buttons'])
def cmd_buttons(message):
    BOT.send_keyboard(message['peer_id'])


@BOT.message_handler(commands=['del_kb'])
def cmd_del_kb(message):
    BOT.remove_keyboard(message['peer_id'])


@BOT.message_handler(commands=['w', 'wf'])
def cmd_w(message):
    full = (message['text'].split()[0] == '/wf')
    if message['peer_id'] in CUR_PARAMS:
        send_screen(message['peer_id'], f'https://ru.wikipedia.org/wiki/{message['text'].split(maxsplit=1)[1]}', full=full)
    else:
        BOT.send_message(message['peer_id'], 'команда доступна только в авторизованном чате')


# Запуск бота
if __name__ == "__main__":
    try:
        BOT.run()
    except KeyboardInterrupt:
        logging.info("Бот остановлен вручную.")
    except Exception as exc:
        logging.critical(f"Критическая ошибка работы бота: {exc}", exc_info=True)
