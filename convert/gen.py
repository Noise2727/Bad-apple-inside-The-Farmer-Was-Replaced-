import cv2
import numpy as np


def rle_encode(flat_array):
    """Сжимает одномерный массив 0 и 1 в формат RLE: [длина, значение, длина, значение...]"""
    if len(flat_array) == 0:
        return []

    # Находим индексы, где значения меняются
    changes = np.where(flat_array[:-1] != flat_array[1:])[0] + 1
    # Считаем длины серий одинаковых элементов
    lengths = np.diff(np.concatenate(([0], changes, [len(flat_array)])))
    # Получаем значения этих серий
    values = flat_array[np.concatenate(([0], changes))]

    # Собираем в один плоский список для компактности: [len1, val1, len2, val2...]
    rle = np.empty(2 * len(lengths), dtype=int)
    rle[0::2] = lengths
    rle[1::2] = values
    return rle.tolist()


def convert_video_to_rle_py(video_path, output_py_path):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print("Ошибка: Не удалось открыть видеофайл.")
        return

    source_fps = cap.get(cv2.CAP_PROP_FPS)
    print(f"Исходная частота кадров видео: {source_fps} FPS")

    # ==========================================
    # НАСТРОЙКИ ОГРАНИЧЕНИЯ И РАЗМЕРА
    # ==========================================
    MAX_COMPRESS_FPS = 23  # Желаемый лимит FPS на выходе (теперь работает точно!)
    TARGET_WIDTH = 100
    TARGET_HEIGHT = 100
    # ==========================================

    # Рассчитываем временные интервалы (в секундах) для точного пропуска кадров
    target_frame_time = 1.0 / MAX_COMPRESS_FPS
    source_frame_time = 1.0 / source_fps

    print(f"Видео будет адаптировано под: {MAX_COMPRESS_FPS} FPS")

    accumulated_time = 0.0
    saved_frames = 0

    with open(output_py_path, 'w', encoding='utf-8') as f:
        # Записываем метаданные, чтобы плеер знал разрешение кадра и правильную скорость
        f.write("# Метаданные видео\n")
        f.write(f"WIDTH = {TARGET_WIDTH}\n")
        f.write(f"HEIGHT = {TARGET_HEIGHT}\n")
        f.write(f"FPS = {MAX_COMPRESS_FPS}\n\n")
        f.write("video = [\n")

        while True:
            ret, frame = cap.read()
            if not ret:
                break  # Конец видео (выходим из цикла естественным образом)

            # Накапливаем время текущего кадра из оригинального видео
            accumulated_time += source_frame_time

            # Если накопилось достаточно времени для целевого кадра (например, >= 1/23 секунды)
            if accumulated_time >= target_frame_time:
                # Списываем время одного целевого кадра
                accumulated_time -= target_frame_time

                # 1. ЧБ -> 2. Изменение размера
                gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                resized_frame = cv2.resize(gray_frame, (TARGET_WIDTH, TARGET_HEIGHT), interpolation=cv2.INTER_AREA)

                # 3. Бинаризация (черный -> 1, белый -> 0)
                _, binary_frame = cv2.threshold(resized_frame, 127, 1, cv2.THRESH_BINARY_INV)

                # 4. Сжатие методом RLE
                flat_list = binary_frame.flatten()
                rle_data = rle_encode(flat_list)

                # Записываем компактную строку RLE-пар
                rle_str = ",".join(map(str, rle_data))
                f.write(f"  [{rle_str}],\n")

                saved_frames += 1
            # Если время не накопилось, кадр просто пропускается (отсекается лишний FPS)

        f.write("]\n")

    cap.release()
    print(f"Успешно сжато кадров: {saved_frames} (видео записано полностью с лимитом FPS)")
    print(f"Результат сохранен в RLE-формате: {output_py_path}")


# Запуск конвертера
convert_video_to_rle_py('inp.mp4', 'video_data.py')
