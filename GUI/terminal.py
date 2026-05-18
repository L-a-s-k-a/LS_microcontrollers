#!/usr/bin/env python3

import time
import logging
import sys

# Библиотека pyOCD для работы с отладчиком и файлом .elf
from pyocd.core.helpers import ConnectHelper
from pyocd.debug.elf.symbols import ELFSymbolProvider
from pyocd.core.target import Target

# --- НАСТРОЙКИ (измените эти параметры!) ---
TARGET_ELF = "../../Installation_Holography/build/Holography.elf"        # Путь к вашему .elf-файлу
VARIABLE_NAME = "GUI_check"          # Имя переменной для мониторинга
# Укажите целевой процессор, если pyOCD не может его определить автоматически.
# Список поддерживаемых целей: `pyocd list --targets`
# Например, 'stm32f103c8' или 'nrf52840'.
TARGET_DEVICE = "stm32f429xi"
# ----------------------------------------

def setup_logging():
    """Настраивает логирование для pyOCD."""
    logging.basicConfig(level=logging.INFO)
    # Подавляем слишком подробные сообщения от pyOCD.
    # Если нужно больше информации, можно изменить уровень на DEBUG
    # или закомментировать строку ниже.
    logging.getLogger('pyocd').setLevel(logging.WARNING)

def main():
    setup_logging()
    
    # 1. Подключаемся к целевой плате.
    # ConnectHelper.session_with_chosen_probe() выбирает первый доступный программатор.
    # Параметр blocking=False позволяет продолжить, если программатор не найден.
    # Если TARGET_DEVICE указан, pyOCD будет использовать его, иначе попытается определить автоматически.
    try:
        session = ConnectHelper.session_with_chosen_probe(
            target_override=TARGET_DEVICE,
            blocking=False,
            # Параметр 'halt' сразу останавливает ядро. 'under-reset' — останавливает при старте.
            connect_mode=Target.ConnectMode.HALT
        )
    except Exception as e:
        print(f"❌ Не удалось создать сессию: {e}")
        print("   Проверьте подключение программатора и питания платы.")
        sys.exit(1)

    if session is None:
        print("❌ Не найден подключенный программатор. Убедитесь, что он подключен.")
        sys.exit(1)

    # Используем контекстный менеджер для автоматического закрытия соединения.
    with session:
        target = session.target
        board = session.board
        print(f"✅ Подключено к устройству: {board.unique_id} (Target: {target.part_number})")

        # 2. Работа с ELF-файлом.
        # Загружаем символы из ELF-файла для поиска адреса переменной по имени.
        if not TARGET_ELF:
            print("❌ Путь к .elf-файлу не указан в настройках.")
            sys.exit(1)

        try:
            # Ассоциируем ELF-файл с целевым устройством.
            target.elf = TARGET_ELF
            symbol_provider = ELFSymbolProvider(target.elf)
            # Получаем адрес переменной по имени.
            var_address = symbol_provider.get_symbol_value(VARIABLE_NAME)
            if var_address is None:
                print(f"❌ Переменная '{VARIABLE_NAME}' не найдена в ELF-файле '{TARGET_ELF}'.")
                sys.exit(1)
            print(f"🔍 Переменная '{VARIABLE_NAME}' найдена по адресу: 0x{var_address:08X}")

            # Попробуем определить тип переменной.
            # Это базовая эвристика: если адрес находится в пределах известных областей памяти.
            # Для более точного определения типа вам нужно знать структуру вашей переменной.
            # Если это стандартный тип (int, float) или структура, читаем её как массив 32-битных слов.
            # По умолчанию читаем 4 байта (32 бита). Если у вас структура или массив, увеличьте значение `size_in_bytes`.
            size_in_bytes = 4  # Допустим, это 32-битное целое число.
            print(f"ℹ️ Чтение {size_in_bytes} байт(а) (интерпретация как 32-битное целое).")

        except Exception as e:
            print(f"❌ Ошибка при загрузке ELF-файла: {e}")
            sys.exit(1)

        # 3. Непрерывный цикл чтения.
        print(f"\n📊 Начинаем мониторинг '{VARIABLE_NAME}' (адрес: 0x{var_address:08X})...")
        print("   Нажмите Ctrl+C для остановки.\n")
        
        try:
            iteration = 0
            while True:
                # Приостанавливаем выполнение ядра для согласованного чтения.
                # Это важно, чтобы переменная не изменилась во время её чтения.
                target.halt()
                
                # Читаем блок памяти.
                # Используем read_memory_block8 для чтения байтов.
                # Если вы знаете, что переменная выровнена, можно использовать read_memory_block32 для скорости.
                data_bytes = target.read_memory_block8(var_address, size_in_bytes)
                
                # Преобразуем байты в целое число (little-endian, т.к. Cortex-M — little-endian архитектура).
                # Для других архитектур может потребоваться другой порядок байт.
                value = int.from_bytes(data_bytes, byteorder='little')
                
                # Продолжаем выполнение ядра.
                target.resume()
                
                # Выводим значение с временной меткой для наглядности.
                print(f"[{iteration:04d}] Значение: {value} (0x{value:08X})")
                
                iteration += 1
                # Пауза между итерациями. Увеличьте, чтобы снизить нагрузку на отладчик.
                # Чем меньше пауза, тем быстрее, но выше риск повлиять на работу МК.
                time.sleep(0.5)
                
        except KeyboardInterrupt:
            print("\n🛑 Мониторинг остановлен пользователем.")
        except Exception as e:
            print(f"\n⚠️ Произошла ошибка во время чтения: {e}")
            print("   Попробуйте перезапустить программу.")

if __name__ == "__main__":
    main()