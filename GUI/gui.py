import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import struct
from dataclasses import dataclass
from typing import List, Dict, Optional, Any
from pathlib import Path
import os

# Библиотеки для работы с .elf файлом и отладкой
# import pyelftools
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection
import pyocd
from pyocd.core.helpers import ConnectHelper
import logging

# Настройка логирования pyocd (можно отключить, если не нужно)
logging.basicConfig(level=logging.WARNING)

# --- Класс для хранения информации о переменной ---
@dataclass
class VariableInfo:
    """Хранит имя, адрес, размер и тип переменной."""
    name: str
    address: int
    size: int
    type_str: str

# --- Класс для парсинга .elf файла ---
class ElfVariableParser:
    """Извлекает информацию о глобальных переменных из .elf файла, используя таблицу символов."""
    def __init__(self, elf_file_path: str):
        self.elf_file_path = elf_file_path
        self.variables: Dict[str, VariableInfo] = {}
        self._parse_elf()

    def _parse_elf(self):
        """Основной метод, который открывает .elf и парсит символы."""
        if not os.path.exists(self.elf_file_path):
            raise FileNotFoundError(f"ELF файл не найден: {self.elf_file_path}")

        with open(self.elf_file_path, 'rb') as f:
            elf = ELFFile(f)
            
            # Проходим по всем секциям в поисках таблицы символов
            for section in elf.iter_sections():
                if isinstance(section, SymbolTableSection):
                    # Перебираем все символы в таблице
                    for symbol in section.iter_symbols():
                        # Фильтруем, чтобы оставить только нужные глобальные переменные
                        # 'STT_OBJECT' — это тип для переменных (данных), в отличие от функций ('STT_FUNC')
                        if symbol['st_info']['type'] == 'STT_OBJECT' and symbol['st_shndx'] != 'SHN_UNDEF':
                            # 'st_size' — это размер переменной в байтах
                            if symbol['st_size'] > 0:
                                # Определяем базовый тип переменной на основе её размера
                                var_type = self._infer_type(symbol['st_size'])
                                
                                var_info = VariableInfo(
                                    name=symbol.name,
                                    address=symbol['st_value'],
                                    size=symbol['st_size'],
                                    type_str=var_type
                                )
                                self.variables[symbol.name] = var_info

    def _infer_type(self, size: int) -> str:
        """Упрощённое определение типа по размеру (можно расширить)."""
        if size == 1:
            return "uint8_t"
        elif size == 2:
            return "uint16_t"
        elif size == 4:
            return "uint32_t"
        else:
            return f"uint8_t[{size}]" # Массив байт

    def get_variable_list(self) -> List[str]:
        """Возвращает отсортированный список имён переменных."""
        return sorted(self.variables.keys())

    def get_variable_info(self, name: str) -> Optional[VariableInfo]:
        """Возвращает информацию о переменной по её имени."""
        return self.variables.get(name)

# --- Основное приложение с GUI ---
class PyOCDExplorer:
    def __init__(self, root):
        self.root = root
        self.root.title("PyOCD Variable Explorer")
        self.root.geometry("800x600")
        
        self.elf_parser: Optional[ElfVariableParser] = None
        self.session: Optional[pyocd.core.session.Session] = None
        self.target: Optional[pyocd.core.target.Target] = None
        
        self._setup_ui()
        self._load_elf_file()
        
        # Привязываем обработчик закрытия окна
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _setup_ui(self):
        """Создаёт все элементы интерфейса."""
        # Верхняя панель с информацией о файле
        top_frame = ttk.Frame(self.root, padding="5")
        top_frame.pack(fill=tk.X)
        
        ttk.Label(top_frame, text="ELF файл:").pack(side=tk.LEFT)
        self.file_label = ttk.Label(top_frame, text="Не выбран", foreground="gray")
        self.file_label.pack(side=tk.LEFT, padx=(5, 0))
        
        # Панель выбора переменной
        select_frame = ttk.LabelFrame(self.root, text="Выбор переменной", padding="10")
        select_frame.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(select_frame, text="Переменная:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.variable_combobox = ttk.Combobox(select_frame, state="readonly", width=50)
        self.variable_combobox.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)
        self.variable_combobox.bind('<<ComboboxSelected>>', self.on_variable_selected)
        
        # Кнопка обновления
        self.refresh_btn = ttk.Button(select_frame, text="Обновить список", command=self.refresh_variable_list)
        self.refresh_btn.grid(row=0, column=2, padx=5, pady=5)
        
        # Кнопка подключения к МК
        self.connect_btn = ttk.Button(select_frame, text="Подключиться к МК", command=self.connect_to_mcu)
        self.connect_btn.grid(row=1, column=0, columnspan=3, pady=5)
        
        # Панель вывода значения
        value_frame = ttk.LabelFrame(self.root, text="Значение переменной", padding="10")
        value_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # Используем scrolledtext для отображения больших объёмов данных
        self.value_text = scrolledtext.ScrolledText(value_frame, height=15, wrap=tk.WORD)
        self.value_text.pack(fill=tk.BOTH, expand=True)

        # --- НОВАЯ ПАНЕЛЬ ЗАПИСИ ---
        write_frame = ttk.LabelFrame(self.root, text="Запись значения", padding="10")
        write_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(write_frame, text="Новое значение:").pack(side=tk.LEFT, padx=5)
        self.write_entry = ttk.Entry(write_frame, width=40)
        self.write_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        
        self.write_btn = ttk.Button(write_frame, text="Записать", command=self.write_variable)
        self.write_btn.pack(side=tk.LEFT, padx=5)
        # -------------------------

        # Статусная строка
        self.status_var = tk.StringVar()
        self.status_var.set("Готов. Выберите ELF файл.")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def _load_elf_file(self):
        """В текущей версии просто открывает файл из диалога."""
        # Для простоты используем диалог выбора файла
        from tkinter import filedialog
        elf_path = filedialog.askopenfilename(
            title="Выберите .elf файл прошивки",
            filetypes=[("ELF files", "*.elf"), ("All files", "*.*")]
        )
        if not elf_path:
            # Если файл не выбран, завершаем работу
            messagebox.showerror("Ошибка", "Не выбран .elf файл. Программа будет закрыта.")
            self.root.quit()
            return
        
        try:
            self.elf_parser = ElfVariableParser(elf_path)
            self.file_label.config(text=os.path.basename(elf_path), foreground="black")
            self.refresh_variable_list()
            self.status_var.set(f"Загружен файл: {elf_path}. Найдено {len(self.elf_parser.variables)} переменных.")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось загрузить ELF файл:\n{e}")
            self.root.quit()

    def refresh_variable_list(self):
        """Обновляет выпадающий список переменными из .elf файла."""
        if self.elf_parser:
            variables = self.elf_parser.get_variable_list()
            self.variable_combobox['values'] = variables
            if variables:
                self.variable_combobox.current(0)
                self.status_var.set(f"Найдено {len(variables)} переменных.")
            else:
                self.status_var.set("Не найдено подходящих переменных в .elf файле.")
        else:
            self.variable_combobox['values'] = []

    def connect_to_mcu(self):
        """Устанавливает соединение с микроконтроллером через PyOCD."""
        if self.session is not None:
            # Если уже подключены, отключаемся
            self.disconnect_from_mcu()
        
        self.status_var.set("Подключение к МК...")
        # Запускаем подключение в отдельном потоке, чтобы не блокировать GUI
        thread = threading.Thread(target=self._connect_thread, daemon=True)
        thread.start()

    def _connect_thread(self):
        try:
            all_probes = ConnectHelper.get_all_connected_probes()
            if not all_probes:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", "Не найден подключённый программатор."))
                self.root.after(0, lambda: self.status_var.set("Ошибка: программатор не найден."))
                return

            my_probe = all_probes[0]
            print(f"Выбран программатор: {my_probe.product_name}")

            session = ConnectHelper.session_with_chosen_probe(
                unique_id=my_probe.unique_id,
                target_override='stm32f429xi',
                options={
                    'auto_unlock': True,
                    'halt_on_connect': True,
                    'primary_core': 0,
                    'allow_no_cores': True
                }
            )
            if session is None:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", "Не удалось создать сессию."))
                self.root.after(0, lambda: self.status_var.set("Ошибка создания сессии."))
                return

            self.session = session
            self.session.open()                         # Явно открываем сессию
            self.target = self.session.board.target
            self.core = self.target.cores[0]            # Берём первое ядро

            # Проверка чтения памяти через ядро
            try:
                self.core.read_memory_block8(0x20000000, 1)
            except Exception as mem_err:
                err_msg = f"Память не доступна:\n{mem_err}"
                print(mem_err)
                self.root.after(0, lambda msg=err_msg: messagebox.showerror("Ошибка", msg))
                self.root.after(0, lambda: self.status_var.set("Ошибка доступа к памяти."))
                self.disconnect_from_mcu()
                return

            self.root.after(0, lambda: self.connect_btn.config(text="Отключиться от МК"))
            self.root.after(0, lambda: self.status_var.set("Подключено. Можно выбирать переменные."))
            self.root.after(0, lambda: messagebox.showinfo("Успех", "Подключение установлено."))

        except Exception as e:
            err_msg = f"Не удалось подключиться:\n{e}"
            self.root.after(0, lambda msg=err_msg: messagebox.showerror("Ошибка", msg))
            self.root.after(0, lambda: self.status_var.set("Ошибка подключения."))

    def disconnect_from_mcu(self):
        if self.core:
            try:
                self.core.resume()      # Возобновляем выполнение кода
            except:
                pass
            self.core = None
        if self.session:
            self.session.close()
            self.session = None
        self.connect_btn.config(text="Подключиться к МК")
        self.status_var.set("Отключено от МК.")

    def on_variable_selected(self, event=None):
        """Обработчик выбора переменной из списка."""
        if self.session is None or self.target is None:
            messagebox.showwarning("Не подключено", "Сначала подключитесь к микроконтроллеру.")
            return
        
        selected = self.variable_combobox.get()
        if not selected:
            return
        
        var_info = self.elf_parser.get_variable_info(selected)
        if var_info is None:
            self.value_text.delete(1.0, tk.END)
            self.value_text.insert(tk.END, "Ошибка: информация о переменной не найдена.")
            return
        
        self.status_var.set(f"Чтение '{selected}' по адресу 0x{var_info.address:X}...")
        # Запускаем чтение в потоке
        thread = threading.Thread(target=self._read_variable_thread, args=(var_info,), daemon=True)
        thread.start()

    def _read_variable_thread(self, var_info: VariableInfo):
        try:
            # Читаем память через self.core (а не self.target)
            data_bytes = self.core.read_memory_block8(var_info.address, var_info.size)
            data = bytes(data_bytes)
            
            value_str = self._interpret_value(data, var_info.type_str, var_info.size)
            
            output = f"Переменная: {var_info.name}\n"
            output += f"Тип: {var_info.type_str}\n"
            output += f"Адрес: 0x{var_info.address:08X}\n"
            output += f"Размер: {var_info.size} байт\n"
            output += f"Raw HEX: {data.hex().upper()}\n"
            output += f"\nЗначение:\n{value_str}"
            
            self.root.after(0, lambda: self._update_value_display(output))
            self.root.after(0, lambda: self.status_var.set(f"Готов. Последнее чтение: {var_info.name}"))
        except Exception as e:
            self.root.after(0, lambda: self._update_value_display(f"Ошибка чтения:\n{str(e)}"))
            self.root.after(0, lambda: self.status_var.set(f"Ошибка при чтении {var_info.name}"))

    def _interpret_value(self, data: bytes, type_str: str, size: int) -> str:
        """Преобразует сырые байты в человеко-читаемый формат на основе предполагаемого типа."""
        # Маленький эндиан, так как это STM32
        if type_str == "uint8_t":
            return str(data[0])
        elif type_str == "uint16_t":
            return str(struct.unpack('<H', data)[0])
        elif type_str == "uint32_t":
            return str(struct.unpack('<I', data)[0])
        elif type_str.startswith("uint8_t["):
            # Для массивов выводим как hex + попытка интерпретации как ASCII строки
            hex_view = ' '.join(f"{b:02X}" for b in data)
            ascii_view = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)
            return f"HEX: {hex_view}\nASCII: {ascii_view}"
        else:
            # Fallback: просто hex дамп
            return ' '.join(f"{b:02X}" for b in data)

    def _update_value_display(self, text: str):
        """Обновляет текстовое поле с результатом."""
        self.value_text.delete(1.0, tk.END)
        self.value_text.insert(tk.END, text)

    def write_variable(self):
        """Обработчик кнопки записи."""
        if self.session is None or self.core is None:
            messagebox.showwarning("Не подключено", "Сначала подключитесь к микроконтроллеру.")
            return

        selected = self.variable_combobox.get()
        if not selected:
            messagebox.showwarning("Нет переменной", "Выберите переменную из списка.")
            return

        var_info = self.elf_parser.get_variable_info(selected)
        if var_info is None:
            messagebox.showerror("Ошибка", "Информация о переменной не найдена.")
            return

        new_value_str = self.write_entry.get().strip()
        if not new_value_str:
            messagebox.showwarning("Пустое значение", "Введите новое значение для записи.")
            return

        self.status_var.set(f"Запись в '{selected}' ...")
        # Запускаем в отдельном потоке, чтобы не блокировать GUI
        thread = threading.Thread(target=self._write_variable_thread, args=(var_info, new_value_str), daemon=True)
        thread.start()

    def _write_variable_thread(self, var_info: VariableInfo, value_str: str):
        """Поток для записи значения в память МК."""
        try:
            # Преобразуем строку в байты в соответствии с типом переменной
            data = self._parse_value_to_bytes(value_str, var_info.type_str, var_info.size)
            if data is None:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", "Не удалось преобразовать значение в байты."))
                self.root.after(0, lambda: self.status_var.set("Ошибка преобразования значения."))
                return

            # Проверяем длину
            if len(data) != var_info.size:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", f"Размер данных ({len(data)} байт) не совпадает с размером переменной ({var_info.size} байт)."))
                self.root.after(0, lambda: self.status_var.set("Ошибка размера данных."))
                return

            # Запись в память
            self.core.write_memory_block8(var_info.address, list(data))
            
            self.root.after(0, lambda: self.status_var.set(f"Успешно записано {len(data)} байт в '{var_info.name}'."))
            # После записи можно автоматически перечитать переменную
            self.root.after(0, self.on_variable_selected)
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Ошибка записи", str(e)))
            self.root.after(0, lambda: self.status_var.set("Ошибка при записи."))

    def _parse_value_to_bytes(self, value_str: str, type_str: str, size: int) -> Optional[bytes]:
        """Преобразует строку ввода в байтовые данные для записи в память.
        Поддерживает:
            - uint8_t: целое число 0-255
            - uint16_t: целое число 0-65535
            - uint32_t: целое число 0-4294967295
            - uint8_t[N]: hex-строка (например, 'AABBCC') или текст (будет дополнен нулями/обрезан)
        """
        try:
            if type_str == "uint8_t":
                val = int(value_str, 0)
                if not (0 <= val <= 255):
                    raise ValueError("Значение вне диапазона uint8_t (0-255)")
                return bytes([val])
            
            elif type_str == "uint16_t":
                val = int(value_str, 0)
                if not (0 <= val <= 65535):
                    raise ValueError("Значение вне диапазона uint16_t (0-65535)")
                return struct.pack('<H', val)   # little-endian
            
            elif type_str == "uint32_t":
                val = int(value_str, 0)
                if not (0 <= val <= 4294967295):
                    raise ValueError("Значение вне диапазона uint32_t (0-4294967295)")
                return struct.pack('<I', val)   # little-endian
            
            elif type_str.startswith("uint8_t["):
                # Для массива байт: пробуем интерпретировать как hex-строку без пробелов,
                # если не получается — пробуем как ASCII строку.
                # Ожидаем, что размер массива = size.
                clean = value_str.replace(" ", "").replace("0x", "").replace(",", "")
                # Пробуем как hex
                if all(c in "0123456789ABCDEFabcdef" for c in clean) and (len(clean) % 2 == 0):
                    data = bytes.fromhex(clean)
                else:
                    # Кодируем как ASCII (или UTF-8) и обрезаем/дополняем до нужного размера
                    data = value_str.encode('ascii', errors='replace')
                
                # Обрезаем или дополняем нулями до нужного размера
                if len(data) > size:
                    data = data[:size]
                elif len(data) < size:
                    data += b'\x00' * (size - len(data))
                return data
            
            else:
                # Неизвестный тип — пробуем hex или просто байты из строки
                raise ValueError(f"Тип {type_str} не поддерживается для записи.")
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Ошибка парсинга", f"Не удалось разобрать значение: {e}"))
            return None
    
    def _on_closing(self):
        """Корректно закрывает соединение перед выходом."""
        self.disconnect_from_mcu()
        self.root.destroy()

# --- Точка входа в программу ---
if __name__ == "__main__":
    root = tk.Tk()
    app = PyOCDExplorer(root)
    root.mainloop()