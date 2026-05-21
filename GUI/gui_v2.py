import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import struct
from dataclasses import dataclass
from typing import List, Dict, Optional, Any
import os
import datetime

# Библиотеки для работы с .elf файлом и отладкой
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection
import pyocd
from pyocd.core.helpers import ConnectHelper
import logging

# Для графиков
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

logging.basicConfig(level=logging.WARNING)


# ==================== Класс для всплывающих подсказок ====================
class ToolTip:
    """Всплывающая подсказка с переносом текста (прямоугольная область)."""
    def __init__(self, widget, text, wraplength=400):
        self.widget = widget
        self.text = text
        self.wraplength = wraplength
        self.tip_window = None
        self.widget.bind('<Enter>', self.enter)
        self.widget.bind('<Leave>', self.leave)

    def enter(self, event=None):
        x, y, _, _ = self.widget.bbox("insert")
        x += self.widget.winfo_rootx() + 20
        y += self.widget.winfo_rooty() + 20
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(tw, text=self.text, justify=tk.LEFT,
                         background="#ffffe0", relief=tk.SOLID, borderwidth=1,
                         font=("tahoma", "10", "normal"),
                         wraplength=self.wraplength)
        label.pack()

    def leave(self, event=None):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None


@dataclass
class VariableInfo:
    name: str
    address: int
    size: int
    type_str: str


class ElfVariableParser:
    def __init__(self, elf_file_path: str):
        self.elf_file_path = elf_file_path
        self.variables: Dict[str, VariableInfo] = {}
        self._parse_elf()

    def _parse_elf(self):
        if not os.path.exists(self.elf_file_path):
            raise FileNotFoundError(f"ELF файл не найден: {self.elf_file_path}")
        with open(self.elf_file_path, 'rb') as f:
            elf = ELFFile(f)
            for section in elf.iter_sections():
                if isinstance(section, SymbolTableSection):
                    for symbol in section.iter_symbols():
                        if symbol['st_info']['type'] == 'STT_OBJECT' and symbol['st_shndx'] != 'SHN_UNDEF':
                            if symbol['st_size'] > 0:
                                var_type = self._infer_type(symbol['st_size'])
                                self.variables[symbol.name] = VariableInfo(
                                    name=symbol.name,
                                    address=symbol['st_value'],
                                    size=symbol['st_size'],
                                    type_str=var_type
                                )

    @staticmethod
    def _infer_type(size: int) -> str:
        if size == 1:
            return "uint8_t"
        if size == 2:
            return "uint16_t"
        if size == 4:
            return "uint32_t"
        return f"uint8_t[{size}]"

    def get_variable_list(self) -> List[str]:
        return sorted(self.variables.keys())

    def get_variable_info(self, name: str) -> Optional[VariableInfo]:
        return self.variables.get(name)


class PyOCDExplorer:
    def __init__(self, root):
        self.root = root
        self.root.title("Лабораторный стенд для Embedded Systems - Управление и мониторинг")
        self.root.geometry("1400x750")
        self.root.resizable(False, False)          # фиксированный размер окна

        # Состояния
        self.auto_read_active = False
        self.auto_write_active = False
        self.auto_read_job = None
        self.auto_write_job = None

        self.elf_parser: Optional[ElfVariableParser] = None
        self.session: Optional[pyocd.core.session.Session] = None
        self.core: Optional[pyocd.core.coresight_target.CortexM] = None

        # Для графиков
        self.plotting_active = False
        self.plotting_job = None
        self.plot_data = {'time': [], 'values': []}
        self.plot_start_time = None
        self.plot_variable_name = None
        self.plot_variable_info = None

        # Демо-режимы
        self.demo_var_name = "demo_mode"
        self.demo_var_info = None

        # Переменные для угла и температуры
        self.angle_var_name = "task_angle"
        self.temp_var_name = "task_temperature"
        self.angle_var_info = None
        self.temp_var_info = None

        self._setup_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

    # ---------------------- Построение интерфейса ----------------------
    def _setup_ui(self):
        main_panel = ttk.Frame(self.root)
        main_panel.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Левая панель (основные функции)
        left_frame = ttk.LabelFrame(main_panel, text="Основное управление", padding="10")
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        # Правая панель (демо-режимы и графики)
        right_frame = ttk.LabelFrame(main_panel, text="Демо-режимы и графики", padding="10")
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))

        # ------------------- Левая панель -------------------
        # Верхняя строка с ELF
        top_frame = ttk.Frame(left_frame)
        top_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(top_frame, text="ELF файл:").pack(side=tk.LEFT)
        self.file_label = ttk.Label(top_frame, text="Не выбран", foreground="gray")
        self.file_label.pack(side=tk.LEFT, padx=(5, 0))
        self.load_elf_btn = ttk.Button(top_frame, text="Загрузить ELF", command=self.load_elf_file)
        self.load_elf_btn.pack(side=tk.RIGHT)

        # Выбор переменной
        select_frame = ttk.LabelFrame(left_frame, text="Выбор переменной", padding="5")
        select_frame.pack(fill=tk.X, pady=5)
        ttk.Label(select_frame, text="Переменная:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=2)
        self.variable_combobox = ttk.Combobox(select_frame, state="readonly", width=40)
        self.variable_combobox.grid(row=0, column=1, sticky=tk.W, padx=5, pady=2)
        self.variable_combobox.bind('<<ComboboxSelected>>', self.on_variable_selected)
        self.refresh_btn = ttk.Button(select_frame, text="Обновить список", command=self.refresh_variable_list)
        self.refresh_btn.grid(row=0, column=2, padx=5, pady=2)

        # Кнопка подключения и индикатор
        conn_frame = ttk.Frame(select_frame)
        conn_frame.grid(row=1, column=0, columnspan=3, pady=5)
        self.connect_btn = ttk.Button(conn_frame, text="Подключиться к МК", command=self.toggle_connection)
        self.connect_btn.pack(side=tk.LEFT, padx=(0, 10))
        self.conn_indicator = tk.Label(conn_frame, text="●", font=("Segoe UI", 12), fg="red")
        self.conn_indicator.pack(side=tk.LEFT)

        # Значение переменной
        value_frame = ttk.LabelFrame(left_frame, text="Значение переменной", padding="5")
        value_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self.value_text = scrolledtext.ScrolledText(value_frame, height=12, wrap=tk.WORD)
        self.value_text.pack(fill=tk.BOTH, expand=True)

        # Запись значения
        write_frame = ttk.LabelFrame(left_frame, text="Запись значения", padding="5")
        write_frame.pack(fill=tk.X, pady=5)
        ttk.Label(write_frame, text="Новое значение:").pack(side=tk.LEFT, padx=5)
        self.write_entry = ttk.Entry(write_frame, width=30)
        self.write_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        self.write_btn = ttk.Button(write_frame, text="Записать", command=self.write_variable)
        self.write_btn.pack(side=tk.LEFT, padx=5)

        # Автоматический режим
        auto_frame = ttk.LabelFrame(left_frame, text="Автоматический режим", padding="5")
        auto_frame.pack(fill=tk.X, pady=5)
        ttk.Label(auto_frame, text="Период (мс):").grid(row=0, column=0, padx=5, pady=2, sticky=tk.W)
        self.period_entry = ttk.Entry(auto_frame, width=10)
        self.period_entry.grid(row=0, column=1, padx=5, pady=2, sticky=tk.W)
        self.period_entry.insert(0, "1000")
        self.auto_read_btn = ttk.Button(auto_frame, text="▶ Старт авточтение", command=self.toggle_auto_read)
        self.auto_read_btn.grid(row=0, column=2, padx=5, pady=2)
        self.auto_write_btn = ttk.Button(auto_frame, text="▶ Старт автозапись", command=self.toggle_auto_write)
        self.auto_write_btn.grid(row=0, column=3, padx=5, pady=2)

        # Статусная строка (общая)
        self.status_var = tk.StringVar()
        self.status_var.set("Готов. Выберите ELF файл.")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        # ------------------- Правая панель -------------------
        # Блок демо-режимов
        demo_block = ttk.LabelFrame(right_frame, text="Управление демонстрационными режимами", padding="10")
        demo_block.pack(fill=tk.X, pady=(0, 10))

        # Режим 1
        frame1 = ttk.Frame(demo_block)
        frame1.pack(fill=tk.X, pady=5)
        self.demo1_btn = ttk.Button(frame1, text="Режим 1: СПР для ДПТ", command=lambda: self.start_demo_mode(1))
        self.demo1_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.demo1_indicator = ttk.Label(frame1, text="○ Не активен", foreground="gray")
        self.demo1_indicator.pack(side=tk.LEFT, padx=5)
        self.help1_label = ttk.Label(frame1, text=" ? ", foreground="blue", cursor="question_arrow")
        self.help1_label.pack(side=tk.LEFT, padx=5)
        ToolTip(self.help1_label,
                "Данный режим демонстрирует работу Системы Подчинённого Регулирования применяемую к Двигателю Постоянного Тока для позиционирования вала привода на заданный угол, расположенного на стенде. Также в данной работе принимают участие непосредственно сам микроконтроллер, драйвер для двигателя, датчики тока и энкодер.")

        frame1b = ttk.Frame(demo_block)
        frame1b.pack(fill=tk.X, pady=2)
        ttk.Label(frame1b, text="Угол (градусы):").pack(side=tk.LEFT, padx=5)
        self.angle_entry = ttk.Entry(frame1b, width=10)
        self.angle_entry.pack(side=tk.LEFT, padx=5)
        self.angle_entry.insert(0, "0")
        self.set_angle_btn = ttk.Button(frame1b, text="Установить угол", width=16)
        self.set_angle_btn.pack(side=tk.LEFT, padx=5)

        # Режим 2
        frame2 = ttk.Frame(demo_block)
        frame2.pack(fill=tk.X, pady=5)
        self.demo2_btn = ttk.Button(frame2, text="Режим 2: Регулятор температуры", command=lambda: self.start_demo_mode(2))
        self.demo2_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.demo2_indicator = ttk.Label(frame2, text="○ Не активен", foreground="gray")
        self.demo2_indicator.pack(side=tk.LEFT, padx=5)
        self.help2_label = ttk.Label(frame2, text=" ? ", foreground="blue", cursor="question_arrow")
        self.help2_label.pack(side=tk.LEFT, padx=5)
        ToolTip(self.help2_label,
                "Данный режим демонстрирует работу релейного регулятора для управления температурой. В данной работе принимают участие нагреватель, вентилятор, релейный модуль и датчик температуры + влажности.")

        frame2b = ttk.Frame(demo_block)
        frame2b.pack(fill=tk.X, pady=2)
        ttk.Label(frame2b, text="Температура (°C):").pack(side=tk.LEFT, padx=5)
        self.temp_entry = ttk.Entry(frame2b, width=10)
        self.temp_entry.pack(side=tk.LEFT, padx=5)
        self.temp_entry.insert(0, "25")
        self.set_temp_btn = ttk.Button(frame2b, text="Установить температуру", width=24)
        self.set_temp_btn.pack(side=tk.LEFT, padx=5)

        # Режим 3
        frame3 = ttk.Frame(demo_block)
        frame3.pack(fill=tk.X, pady=5)
        self.demo3_btn = ttk.Button(frame3, text="Режим 3: Светодиодная матрица", command=lambda: self.start_demo_mode(3))
        self.demo3_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.demo3_indicator = ttk.Label(frame3, text="○ Не активен", foreground="gray")
        self.demo3_indicator.pack(side=tk.LEFT, padx=5)
        self.help3_label = ttk.Label(frame3, text=" ? ", foreground="blue", cursor="question_arrow")
        self.help3_label.pack(side=tk.LEFT, padx=5)
        ToolTip(self.help3_label,
                "Данный режим демонстрирует функциональность светодиодной матрицы P10: вывод бегущей строки с заданным текстом.")

        frame3b = ttk.Frame(demo_block)
        frame3b.pack(fill=tk.X, pady=2)
        ttk.Label(frame3b, text="Текст бегущей строки:").pack(side=tk.LEFT, padx=5)
        self.marquee_text_entry = ttk.Entry(frame3b, width=30)
        self.marquee_text_entry.pack(side=tk.LEFT, padx=5)
        self.marquee_text_entry.insert(0, "Hello World!")
        self.set_marquee_btn = ttk.Button(frame3b, text="Установить текст", width=18)
        self.set_marquee_btn.pack(side=tk.LEFT, padx=5)

        # Блок графиков
        plot_block = ttk.LabelFrame(right_frame, text="График изменения переменной во времени", padding="10")
        plot_block.pack(fill=tk.BOTH, expand=True)

        plot_controls = ttk.Frame(plot_block)
        plot_controls.pack(fill=tk.X, pady=5)
        ttk.Label(plot_controls, text="Переменная:").pack(side=tk.LEFT, padx=5)
        self.plot_var_combobox = ttk.Combobox(plot_controls, state="readonly", width=25)
        self.plot_var_combobox.pack(side=tk.LEFT, padx=5)
        self.start_plot_btn = ttk.Button(plot_controls, text="Начать сбор", command=self.start_plotting)
        self.start_plot_btn.pack(side=tk.LEFT, padx=5)
        self.stop_plot_btn = ttk.Button(plot_controls, text="Остановить", command=self.stop_plotting, state=tk.DISABLED)
        self.stop_plot_btn.pack(side=tk.LEFT, padx=5)
        self.clear_plot_btn = ttk.Button(plot_controls, text="Очистить", command=self.clear_plot)
        self.clear_plot_btn.pack(side=tk.LEFT, padx=5)

        self.fig = Figure(figsize=(5, 4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("Время (с)")
        self.ax.set_ylabel("Значение")
        self.ax.grid(True)
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_block)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    # ---------------------- Демо-режимы и дополнительные функции ----------------------
    def start_demo_mode(self, mode: int):
        """Активация демо-режима (запись в demo_mode)."""
        if not self._check_connection_and_elf("запуска демо-режима"):
            return
        if self.demo_var_info is None:
            self.demo_var_info = self.elf_parser.get_variable_info(self.demo_var_name)
            if self.demo_var_info is None:
                messagebox.showerror("Ошибка", f"Переменная '{self.demo_var_name}' не найдена в ELF.")
                return
        if mode == 3:
            self.set_marquee_text()   # предварительно отправим текст
        try:
            data = self._parse_value_to_bytes(str(mode), self.demo_var_info.type_str, self.demo_var_info.size)
            if data is None:
                raise ValueError("Ошибка преобразования")
            self.core.write_memory_block8(self.demo_var_info.address, list(data))
            self.status_var.set(f"Демо-режим {mode} активирован.")
            self._update_demo_indicator(mode)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось активировать режим {mode}:\n{e}")

    def _update_demo_indicator(self, active_mode: int):
        for mode, label in [(1, self.demo1_indicator), (2, self.demo2_indicator), (3, self.demo3_indicator)]:
            if mode == active_mode:
                label.config(text="● Активен", foreground="green")
            else:
                label.config(text="○ Не активен", foreground="gray")

    def set_angle(self):
        """Отправляет значение угла в переменную task_angle."""
        if not self._check_connection_and_elf("установки угла"):
            return
        if self.angle_var_info is None:
            self.angle_var_info = self.elf_parser.get_variable_info(self.angle_var_name)
            if self.angle_var_info is None:
                messagebox.showerror("Ошибка", f"Переменная '{self.angle_var_name}' (int32_t) не найдена в ELF.")
                return
        try:
            angle = int(self.angle_entry.get().strip())
            data = struct.pack('<i', angle)
            if len(data) != self.angle_var_info.size:
                messagebox.showerror("Ошибка", f"Размер переменной {self.angle_var_info.size} байт, а передано {len(data)}")
                return
            self.core.write_memory_block8(self.angle_var_info.address, list(data))
            self.status_var.set(f"Угол установлен: {angle}°")
        except ValueError:
            messagebox.showerror("Ошибка", "Введите целое число (угол в градусах).")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось записать угол:\n{e}")

    def set_temperature(self):
        """Отправляет значение температуры в переменную task_temperature (int32_t, единицы - градусы)."""
        if not self._check_connection_and_elf("установки температуры"):
            return
        if self.temp_var_info is None:
            self.temp_var_info = self.elf_parser.get_variable_info(self.temp_var_name)
            if self.temp_var_info is None:
                messagebox.showerror("Ошибка", f"Переменная '{self.temp_var_name}' (int32_t) не найдена в ELF.")
                return
        try:
            temp_c = float(self.temp_entry.get().strip())
            temp_int = int(round(temp_c))
            data = struct.pack('<i', temp_int)
            if len(data) != self.temp_var_info.size:
                messagebox.showerror("Ошибка", f"Размер переменной {self.temp_var_info.size} байт, а передано {len(data)}")
                return
            self.core.write_memory_block8(self.temp_var_info.address, list(data))
            self.status_var.set(f"Температура установлена: {temp_int}°C")
        except ValueError:
            messagebox.showerror("Ошибка", "Введите число (температура в °C).")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось записать температуру:\n{e}")

    def set_marquee_text(self):
        """Отправляет текст для бегущей строки (переменная marquee_text)."""
        if not self._check_connection_and_elf("установки текста"):
            return
        text = self.marquee_text_entry.get().strip()
        if not text:
            messagebox.showwarning("Нет текста", "Введите текст для бегущей строки.")
            return
        marquee_var_name = "marquee_text"
        var_info = self.elf_parser.get_variable_info(marquee_var_name)
        if var_info is None:
            messagebox.showwarning("Не найдено", f"Переменная '{marquee_var_name}' отсутствует в ELF.")
            return
        data = text.encode('ascii', errors='replace')[:var_info.size]
        if len(data) < var_info.size:
            data += b'\x00' * (var_info.size - len(data))
        try:
            self.core.write_memory_block8(var_info.address, list(data))
            self.status_var.set(f"Текст отправлен: {text}")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось записать текст:\n{e}")

    # ---------------------- Графики ----------------------
    def start_plotting(self):
        if not self._check_connection_and_elf("построения графика"):
            return
        var_name = self.plot_var_combobox.get()
        if not var_name:
            messagebox.showwarning("Нет переменной", "Выберите переменную для графика.")
            return
        var_info = self.elf_parser.get_variable_info(var_name)
        if var_info is None:
            messagebox.showerror("Ошибка", f"Информация о переменной '{var_name}' не найдена.")
            return
        self.stop_plotting()
        self.plotting_active = True
        self.plot_variable_name = var_name
        self.plot_variable_info = var_info
        self.plot_data = {'time': [], 'values': []}
        self.plot_start_time = datetime.datetime.now()
        self.start_plot_btn.config(state=tk.DISABLED)
        self.stop_plot_btn.config(state=tk.NORMAL)
        self.status_var.set(f"Сбор данных для графика: {var_name}")
        self._schedule_plot_update()

    def stop_plotting(self):
        self.plotting_active = False
        if self.plotting_job:
            self.root.after_cancel(self.plotting_job)
            self.plotting_job = None
        self.start_plot_btn.config(state=tk.NORMAL)
        self.stop_plot_btn.config(state=tk.DISABLED)
        self.status_var.set("Сбор данных для графика остановлен.")

    def clear_plot(self):
        self.stop_plotting()
        self.plot_data = {'time': [], 'values': []}
        self.ax.clear()
        self.ax.set_xlabel("Время (с)")
        self.ax.set_ylabel("Значение")
        self.ax.grid(True)
        self.canvas.draw()
        self.status_var.set("График очищен.")

    def _schedule_plot_update(self):
        if not self.plotting_active:
            return
        self._update_plot_data()
        period = self._get_period()
        self.plotting_job = self.root.after(period, self._schedule_plot_update)

    def _update_plot_data(self):
        if not self.plotting_active or self.session is None or self.core is None or self.plot_variable_info is None:
            return
        try:
            data_bytes = self.core.read_memory_block8(self.plot_variable_info.address, self.plot_variable_info.size)
            data = bytes(data_bytes)
            value = self._bytes_to_number(data, self.plot_variable_info.type_str)
            now = datetime.datetime.now()
            delta = (now - self.plot_start_time).total_seconds()
            self.plot_data['time'].append(delta)
            self.plot_data['values'].append(value)
            if len(self.plot_data['time']) > 500:
                self.plot_data['time'] = self.plot_data['time'][-500:]
                self.plot_data['values'] = self.plot_data['values'][-500:]
            self.root.after(0, self._redraw_plot)
        except Exception as e:
            print(f"Ошибка сбора данных: {e}")

    def _bytes_to_number(self, data: bytes, type_str: str) -> float:
        if type_str == "uint8_t":
            return data[0]
        if type_str == "uint16_t":
            return struct.unpack('<H', data)[0]
        if type_str == "uint32_t":
            return struct.unpack('<I', data)[0]
        return 0.0

    def _redraw_plot(self):
        if not self.plot_data['time']:
            return
        self.ax.clear()
        self.ax.plot(self.plot_data['time'], self.plot_data['values'], 'b-', linewidth=1)
        self.ax.set_xlabel("Время (с)")
        self.ax.set_ylabel("Значение")
        self.ax.set_title(f"{self.plot_variable_name} от времени")
        self.ax.grid(True)
        self.canvas.draw()

    # ---------------------- Основные функции ----------------------
    def _check_connection_and_elf(self, action: str) -> bool:
        if self.session is None or self.core is None:
            messagebox.showwarning("Не подключено", f"Для {action} подключитесь к МК.")
            return False
        if self.elf_parser is None:
            messagebox.showwarning("Нет ELF", f"Для {action} загрузите .elf файл.")
            return False
        return True

    def load_elf_file(self):
        self.stop_auto_read()
        self.stop_auto_write()
        self.stop_plotting()
        from tkinter import filedialog
        elf_path = filedialog.askopenfilename(filetypes=[("ELF files", "*.elf")])
        if not elf_path:
            return
        if self.session is not None:
            self.disconnect_from_mcu()
        try:
            self.elf_parser = ElfVariableParser(elf_path)
            self.file_label.config(text=os.path.basename(elf_path), foreground="black")
            self.refresh_variable_list()
            var_list = self.elf_parser.get_variable_list()
            self.plot_var_combobox['values'] = var_list
            if var_list:
                self.plot_var_combobox.current(0)

            self.demo_var_info = self.elf_parser.get_variable_info(self.demo_var_name)
            self.angle_var_info = self.elf_parser.get_variable_info(self.angle_var_name)
            self.temp_var_info = self.elf_parser.get_variable_info(self.temp_var_name)

            self.status_var.set(f"Загружен {elf_path}. Найдено {len(self.elf_parser.variables)} переменных.")
            self.value_text.delete(1.0, tk.END)
            self.write_entry.delete(0, tk.END)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось загрузить ELF:\n{e}")
            self.file_label.config(text="Ошибка", foreground="red")
            self.elf_parser = None
            self.variable_combobox['values'] = []
            self.plot_var_combobox['values'] = []

    def refresh_variable_list(self):
        if self.elf_parser:
            vars_list = self.elf_parser.get_variable_list()
            self.variable_combobox['values'] = vars_list
            if vars_list:
                self.variable_combobox.current(0)
            self.status_var.set(f"Найдено {len(vars_list)} переменных.")
        else:
            self.variable_combobox['values'] = []

    def toggle_connection(self):
        if self.session is not None:
            self.disconnect_from_mcu()
        else:
            self.connect_to_mcu()

    def connect_to_mcu(self):
        if self.session is not None:
            self.disconnect_from_mcu()
        self.status_var.set("Подключение...")
        threading.Thread(target=self._connect_thread, daemon=True).start()

    def _connect_thread(self):
        try:
            probes = ConnectHelper.get_all_connected_probes()
            if not probes:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", "Программатор не найден."))
                return
            session = ConnectHelper.session_with_chosen_probe(
                unique_id=probes[0].unique_id,
                target_override='stm32f429xi',
                options={'auto_unlock': True, 'halt_on_connect': False}
            )
            if session is None:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", "Не удалось создать сессию."))
                return
            self.session = session
            self.session.open()
            self.target = self.session.board.target
            self.core = self.target.cores[0]
            self.core.read_memory_block8(0x20000000, 1)  # проверка доступа
            self.root.after(0, lambda: self.connect_btn.config(text="Отключиться от МК"))
            self.root.after(0, lambda: self.status_var.set("Подключено."))
            self.root.after(0, lambda: self.conn_indicator.config(fg="green"))

            if self.elf_parser:
                self.demo_var_info = self.elf_parser.get_variable_info(self.demo_var_name)
                self.angle_var_info = self.elf_parser.get_variable_info(self.angle_var_name)
                self.temp_var_info = self.elf_parser.get_variable_info(self.temp_var_name)
                if self.demo_var_info is None:
                    self.root.after(0, lambda: messagebox.showwarning("Предупреждение", f"Переменная {self.demo_var_name} не найдена"))
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Ошибка", f"Подключение не удалось:\n{e}"))

    def disconnect_from_mcu(self):
        self.stop_auto_read()
        self.stop_auto_write()
        self.stop_plotting()
        if self.core:
            try:
                self.core.resume()
            except:
                pass
            self.core = None
        if self.session:
            self.session.close()
            self.session = None
        self.connect_btn.config(text="Подключиться к МК")
        self.status_var.set("Отключено.")
        self.conn_indicator.config(fg="red")
        self._update_demo_indicator(0)

    def on_variable_selected(self, event=None):
        if self.auto_read_active:
            self.stop_auto_read()
        if not self._check_connection_and_elf("чтения"):
            return
        selected = self.variable_combobox.get()
        if not selected:
            return
        var_info = self.elf_parser.get_variable_info(selected)
        if not var_info:
            self.value_text.delete(1.0, tk.END)
            self.value_text.insert(tk.END, "Ошибка: переменная не найдена.")
            return
        self.status_var.set(f"Чтение {selected}...")
        threading.Thread(target=self._read_variable_thread, args=(var_info,), daemon=True).start()

    def _read_variable_thread(self, var_info: VariableInfo):
        try:
            data_bytes = self.core.read_memory_block8(var_info.address, var_info.size)
            data = bytes(data_bytes)
            value_str = self._interpret_value(data, var_info.type_str, var_info.size)
            output = (f"Переменная: {var_info.name}\nТип: {var_info.type_str}\n"
                      f"Адрес: 0x{var_info.address:08X}\nРазмер: {var_info.size}\n"
                      f"HEX: {data.hex().upper()}\n\nЗначение:\n{value_str}")
            self.root.after(0, lambda: self._update_value_display(output))
            self.root.after(0, lambda: self.status_var.set(f"Готово: {var_info.name}"))
        except Exception as e:
            self.root.after(0, lambda: self._update_value_display(f"Ошибка чтения:\n{e}"))

    def _interpret_value(self, data: bytes, type_str: str, size: int) -> str:
        if type_str == "uint8_t":
            return str(data[0])
        if type_str == "uint16_t":
            return str(struct.unpack('<H', data)[0])
        if type_str == "uint32_t":
            return str(struct.unpack('<I', data)[0])
        if type_str.startswith("uint8_t["):
            hex_view = ' '.join(f"{b:02X}" for b in data)
            ascii_view = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)
            return f"HEX: {hex_view}\nASCII: {ascii_view}"
        return ' '.join(f"{b:02X}" for b in data)

    def _update_value_display(self, text: str):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.value_text.insert(tk.END, f"\n[{timestamp}]\n{text}\n{'-'*50}\n")
        self.value_text.see(tk.END)

    def write_variable(self):
        if not self._check_connection_and_elf("записи"):
            return
        selected = self.variable_combobox.get()
        if not selected:
            messagebox.showwarning("Нет переменной", "Выберите переменную.")
            return
        var_info = self.elf_parser.get_variable_info(selected)
        if not var_info:
            messagebox.showerror("Ошибка", "Информация о переменной не найдена.")
            return
        new_value = self.write_entry.get().strip()
        if not new_value:
            messagebox.showwarning("Пусто", "Введите значение.")
            return
        self.status_var.set(f"Запись в {selected}...")
        threading.Thread(target=self._write_variable_thread, args=(var_info, new_value), daemon=True).start()

    def _write_variable_thread(self, var_info: VariableInfo, value_str: str):
        try:
            data = self._parse_value_to_bytes(value_str, var_info.type_str, var_info.size)
            if data is None or len(data) != var_info.size:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", "Неверный размер данных."))
                return
            self.core.write_memory_block8(var_info.address, list(data))
            self.root.after(0, lambda: self.status_var.set(f"Записано {len(data)} байт в {var_info.name}"))
            self.root.after(0, self.on_variable_selected)
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Ошибка записи", str(e)))

    def _parse_value_to_bytes(self, value_str: str, type_str: str, size: int) -> Optional[bytes]:
        try:
            if type_str == "uint8_t":
                return bytes([int(value_str, 0) & 0xFF])
            if type_str == "uint16_t":
                return struct.pack('<H', int(value_str, 0) & 0xFFFF)
            if type_str == "uint32_t":
                return struct.pack('<I', int(value_str, 0) & 0xFFFFFFFF)
            if type_str.startswith("uint8_t["):
                clean = value_str.replace(" ", "").replace("0x", "")
                if all(c in "0123456789ABCDEFabcdef" for c in clean) and len(clean) % 2 == 0:
                    data = bytes.fromhex(clean)
                else:
                    data = value_str.encode('ascii', errors='replace')
                if len(data) > size:
                    data = data[:size]
                else:
                    data += b'\x00' * (size - len(data))
                return data
            raise ValueError(f"Тип {type_str} не поддерживается")
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Ошибка", f"Не удалось разобрать значение: {e}"))
            return None

    # ---------------------- Автоматические режимы ----------------------
    def toggle_auto_read(self):
        if self.auto_read_active:
            self.stop_auto_read()
        else:
            self.start_auto_read()

    def start_auto_read(self):
        if not self._check_ready_for_auto("чтения"):
            return
        self.auto_read_active = True
        self.auto_read_btn.config(text="⏹ Стоп авточтение")
        self._schedule_auto_read()

    def stop_auto_read(self):
        self.auto_read_active = False
        if self.auto_read_job:
            self.root.after_cancel(self.auto_read_job)
            self.auto_read_job = None
        self.auto_read_btn.config(text="▶ Старт авточтение")

    def _schedule_auto_read(self):
        if not self.auto_read_active:
            return
        self._auto_read_iteration()
        self.auto_read_job = self.root.after(self._get_period(), self._schedule_auto_read)

    def _auto_read_iteration(self):
        if not self.session or not self.core or not self.elf_parser:
            self.stop_auto_read()
            return
        selected = self.variable_combobox.get()
        if not selected:
            self.stop_auto_read()
            return
        var_info = self.elf_parser.get_variable_info(selected)
        if not var_info:
            self.stop_auto_read()
            return
        threading.Thread(target=self._read_variable_thread, args=(var_info,), daemon=True).start()

    def toggle_auto_write(self):
        if self.auto_write_active:
            self.stop_auto_write()
        else:
            self.start_auto_write()

    def start_auto_write(self):
        if not self._check_ready_for_auto("записи"):
            return
        if not self.write_entry.get().strip():
            messagebox.showwarning("Нет значения", "Введите значение для автозаписи.")
            return
        self.auto_write_active = True
        self.auto_write_btn.config(text="⏹ Стоп автозапись")
        self._schedule_auto_write()

    def stop_auto_write(self):
        self.auto_write_active = False
        if self.auto_write_job:
            self.root.after_cancel(self.auto_write_job)
            self.auto_write_job = None
        self.auto_write_btn.config(text="▶ Старт автозапись")

    def _schedule_auto_write(self):
        if not self.auto_write_active:
            return
        self._auto_write_iteration()
        self.auto_write_job = self.root.after(self._get_period(), self._schedule_auto_write)

    def _auto_write_iteration(self):
        if not self.session or not self.core or not self.elf_parser:
            self.stop_auto_write()
            return
        selected = self.variable_combobox.get()
        if not selected:
            self.stop_auto_write()
            return
        var_info = self.elf_parser.get_variable_info(selected)
        if not var_info:
            self.stop_auto_write()
            return
        value = self.write_entry.get().strip()
        if value:
            threading.Thread(target=self._write_variable_thread, args=(var_info, value), daemon=True).start()

    def _check_ready_for_auto(self, action):
        if self.session is None or self.core is None:
            messagebox.showwarning("Не подключено", f"Подключитесь к МК для {action}.")
            return False
        if self.elf_parser is None:
            messagebox.showwarning("Нет ELF", f"Загрузите .elf файл для {action}.")
            return False
        if not self.variable_combobox.get():
            messagebox.showwarning("Нет переменной", f"Выберите переменную для {action}.")
            return False
        return True

    def _get_period(self):
        try:
            return max(50, int(self.period_entry.get()))
        except:
            return 1000

    def _on_closing(self):
        self.stop_auto_read()
        self.stop_auto_write()
        self.stop_plotting()
        self.disconnect_from_mcu()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = PyOCDExplorer(root)
    root.mainloop()