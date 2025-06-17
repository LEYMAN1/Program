import cv2
import numpy as np
import os
from tkinter import Tk, Label, filedialog, messagebox, Frame, Canvas, Entry, Toplevel
from tkinter import ttk
from PIL import Image, ImageTk

class ImageProcessingApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Программа обработки изображений")
        self.root.geometry("800x600")

        # Создаем фрейм для кнопок и изображения
        self.image_frame = Frame(root, width=800, height=400, bg='gray')
        self.image_frame.pack(expand=True, fill='both')

        # Помещаем холст для изображения
        self.canvas = Canvas(self.image_frame, bg='gray')
        self.canvas.pack(expand=True, fill='both')

        # Фрейм для кнопок
        self.button_frame = Frame(root)
        self.button_frame.pack(side='bottom', fill='x')

        # Центрируем фрейм с кнопками
        self.button_inner_frame = Frame(self.button_frame)
        self.button_inner_frame.pack(expand=True)

        # Настройка стиля для закругленных кнопок с черным текстом
        style = ttk.Style()
        style.configure("Rounded.TButton",
                        relief="flat",
                        borderwidth=0,
                        background="#008CBA",
                        foreground="black",
                        padding=6,
                        anchor="center")
        style.map("Rounded.TButton",
                  background=[("active", "#005F6A")],
                  relief=[("pressed", "sunken")])

        # Кнопки первого ряда
        self.create_button(self.button_inner_frame, "Загрузить", self.load_image, 0, 0)
        self.create_button(self.button_inner_frame, "Сделать снимок", self.capture_image, 0, 1)
        self.create_button(self.button_inner_frame, "По умолчанию", self.show_original, 0, 2)
        self.create_button(self.button_inner_frame, "Сохранить", self.save_image, 0, 3)

        # Кнопка второго ряда
        self.create_button(self.button_inner_frame, "Инвертировать", self.toggle_negative, 1, 0, colspan=4)

        # Кнопки третьего ряда
        self.create_button(self.button_inner_frame, "Обрезать", self.crop_image, 2, 0)
        self.create_button(self.button_inner_frame, "Вращать", self.rotate_image, 2, 1)
        self.create_button(self.button_inner_frame, "Прямоугольник", self.draw_rectangle, 2, 2)

        # Кнопки четвертого ряда
        self.create_button(self.button_inner_frame, "Красный канал", lambda: self.show_channel(2), 3, 0)
        self.create_button(self.button_inner_frame, "Зеленый канал", lambda: self.show_channel(1), 3, 1)
        self.create_button(self.button_inner_frame, "Синий канал", lambda: self.show_channel(0), 3, 2)

        # Настройки изображения
        self.image = None
        self.original_image = None
        self.negative = False

        # Привязываем событие изменения размера окна
        self.root.bind('<Configure>', self.resize_image)

    def create_button(self, parent, text, command, row, column, colspan=1):
        button = ttk.Button(parent, text=text, command=command, style="Rounded.TButton")
        button.grid(row=row, column=column, columnspan=colspan, padx=5, pady=5, sticky="ew")
        parent.grid_columnconfigure(column, weight=1)

    def load_image(self):
        file_path = filedialog.askopenfilename(filetypes=[("Image files", "*.jpg;*.png")])
        if file_path:
            # Убедитесь, что путь к файлу кодируется правильно
            file_path = os.path.abspath(file_path)
            self.image = cv2.imdecode(np.fromfile(file_path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
            if self.image is not None:
                self.original_image = self.image.copy()
                self.show_image(self.image)
            else:
                messagebox.showerror("Ошибка", "Не удалось загрузить изображение. Проверьте путь к файлу.")

    def save_image(self):
        if self.image is not None:
            file_path = filedialog.asksaveasfilename(defaultextension=".jpg", filetypes=[("JPEG files", "*.jpg"), ("PNG files", "*.png")])
            if file_path:
                cv2.imwrite(file_path, self.image)
                messagebox.showinfo("Успех", "Изображение успешно сохранено!")

    def capture_image(self):
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            messagebox.showerror("Ошибка", "Нет веб-камеры")
            return
        ret, frame = cap.read()
        cap.release()
        if ret:
            self.image = frame
            self.original_image = self.image.copy()
            self.show_image(self.image)
        else:
            messagebox.showerror("Ошибка", "Не удалось сделать снимок")

    def show_image(self, img):
        self.canvas.delete("all")
        max_size = (self.image_frame.winfo_width(), self.image_frame.winfo_height())
        scale = min(max_size[0] / img.shape[1], max_size[1] / img.shape[0])
        new_size = (int(img.shape[1] * scale), (int(img.shape[0] * scale)))
        resized_img = cv2.resize(img, new_size)

        b, g, r = cv2.split(resized_img)
        img = cv2.merge((r, g, b))
        im = Image.fromarray(img)
        imgtk = ImageTk.PhotoImage(image=im)

        self.canvas.create_image(max_size[0] // 2, max_size[1] // 2, anchor='center', image=imgtk)
        self.canvas.imgtk = imgtk

    def resize_image(self, event):
        if self.image is not None:
            self.show_image(self.image)

    def show_channel(self, channel):
        if self.image is not None:
            channel_img = np.zeros_like(self.image)
            channel_img[:, :, channel] = self.image[:, :, channel]
            self.show_image(channel_img)

    def toggle_negative(self):
        if self.image is not None:
            if self.negative:
                self.show_image(self.original_image)
            else:
                negative_img = cv2.bitwise_not(self.image)
                self.show_image(negative_img)
            self.negative = not self.negative

    def show_original(self):
        if self.original_image is not None:
            self.image = self.original_image.copy()
            self.negative = False
            self.show_image(self.image)

    def crop_image(self):
        if self.image is not None:
            self.param_window("Обрезка изображения", ["x", "y", "ширина", "высота"], self.perform_crop)

    def rotate_image(self):
        if self.image is not None:
            self.param_window("Вращение изображения", ["угол"], self.perform_rotate)

    def draw_rectangle(self):
        if self.image is not None:
            self.param_window("Рисование прямоугольника", ["x", "y", "ширина", "высота"], self.perform_draw_rectangle)

    def param_window(self, title, labels, command):
        window = Toplevel(self.root)
        window.title(title)
        entries = {}
        for i, label in enumerate(labels):
            Label(window, text=label).grid(row=i, column=0)
            entry = Entry(window)
            entry.grid(row=i, column=1)
            entries[label] = entry
        ttk.Button(window, text="OK", command=lambda: command(window, entries), style="Rounded.TButton").grid(row=len(labels), columnspan=2)

    def perform_crop(self, window, entries):
        try:
            x = int(entries["x"].get())
            y = int(entries["y"].get())
            w = int(entries["ширина"].get())
            h = int(entries["высота"].get())
            cropped_img = self.image[y:y+h, x:x+w]
            self.image = cropped_img
            self.show_image(self.image)
            window.destroy()
        except Exception as e:
            messagebox.showerror("Ошибка", "Неверные параметры: " + str(e))

    def perform_rotate(self, window, entries):
        try:
            angle = float(entries["угол"].get())
            (h, w) = self.image.shape[:2]
            center = (w // 2, h // 2)
            M = cv2.getRotationMatrix2D(center, angle, 1.0)
            rotated_img = cv2.warpAffine(self.image, M, (w, h))
            self.image = rotated_img
            self.show_image(self.image)
            window.destroy()
        except Exception as e:
            messagebox.showerror("Ошибка", "Неверные параметры: " + str(e))

    def perform_draw_rectangle(self, window, entries):
        try:
            x = int(entries["x"].get())
            y = int(entries["y"].get())
            w = int(entries["ширина"].get())
            h = int(entries["высота"].get())
            img_copy = self.image.copy()
            cv2.rectangle(img_copy, (x, y), (x+w, y+h), (255, 0, 0), 2)
            self.image = img_copy
            self.show_image(self.image)
            window.destroy()
        except Exception as e:
            messagebox.showerror("Ошибка", "Неверные параметры: " + str(e))

if __name__ == "__main__":
    root = Tk()
    app = ImageProcessingApp(root)
    root.mainloop()
