import pyocd
import struct
import logging
from pyocd.core.helpers import ConnectHelper

# Включим логирование, чтобы видеть, что происходит (опционально)
logging.basicConfig(level=logging.INFO)

# --- Замените адрес и размер на ваши ---
VARIABLE_ADDRESS = 0x20000028
VARIABLE_SIZE = 4

# 1. Подключаемся к программатору и целевому устройству
#    Код внутри блока 'with session' автоматически откроет и закроет сессию.
with ConnectHelper.session_with_chosen_probe() as session:
    target = session.target

    # 2. Останавливаем выполнение кода на микроконтроллере
    target.halt()

    # 3. Читаем память по известному адресу
    #    read_memory_block8 возвращает список целых чисел (байт)
    data_list = target.read_memory_block8(VARIABLE_ADDRESS, VARIABLE_SIZE)
    data = bytes(data_list) # Преобразуем список байт в bytes-объект

    # 4. Преобразуем байты в нужный тип (например, 32-битное целое)
    #    '<I' означает little-endian, 4-байтовое целое без знака
    my_variable = struct.unpack('<I', data)[0]

    # 5. Выводим значение
    print(f"Значение переменной по адресу 0x{VARIABLE_ADDRESS:X}: {my_variable}")

    # 6. Продолжаем выполнение кода на микроконтроллере
    target.resume()