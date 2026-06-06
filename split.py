import os
import random
import shutil

IMAGE_DIR = "synthetic/images/train"
LABEL_DIR = "synthetic/labels/train"

VAL_IMAGE_DIR = "synthetic/images/val"
VAL_LABEL_DIR = "synthetic/labels/val"

SPLIT_RATIO = 0.2  # 20% validation

os.makedirs(VAL_IMAGE_DIR, exist_ok=True)
os.makedirs(VAL_LABEL_DIR, exist_ok=True)

images = [f for f in os.listdir(IMAGE_DIR) if f.endswith(".jpg")]

random.shuffle(images)

val_count = int(len(images) * SPLIT_RATIO)

val_images = images[:val_count]

for img_name in val_images:
    label_name = img_name.replace(".jpg", ".txt")

    shutil.move(
        os.path.join(IMAGE_DIR, img_name),
        os.path.join(VAL_IMAGE_DIR, img_name)
    )

    label_path = os.path.join(LABEL_DIR, label_name)
    if os.path.exists(label_path):
        shutil.move(
            label_path,
            os.path.join(VAL_LABEL_DIR, label_name)
        )

print(f"Moved {val_count} images to validation set.")
print("Done.")