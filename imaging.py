"""附圖存進資料庫之前，在伺服器重新存一次 JPEG。

web 會先在瀏覽器裡把圖縮成 480P 再上傳，那一步也會順便丟掉照片裡的 EXIF（拍攝位置、
相機型號、拍攝時間）。但任何人都可以不經過網頁、直接呼叫 API 上傳原始的照片，所以伺服器
不能假設圖片已經處理過：一律解開來，只留下畫素，重新存一次。這樣存進去的圖不會帶任何
中繼資料，也一定是讀得出來的 JPEG。

解開之前先看圖片宣稱的尺寸。一張檔案很小、尺寸卻很大的圖（例如兩萬乘兩萬），解開時會吃掉
好幾 GB 的記憶體，所以尺寸太大的直接拒絕，不去解它。
"""

from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

# 480P 的圖短邊只有 480，全景照長邊可能好幾千（480 × 8000 是 384 萬畫素）。這兩個上限
# 只用來擋惡意的檔案。一張 400 萬畫素的圖解開大約佔 12 MB，服務只有 512 MB 的記憶體，
# 而且同時會有好幾個請求，所以不能抓得太寬。
MAX_PIXELS = 4_000_000
MAX_SIDE = 8_000
# 重新存檔的品質從高試到低，直到檔案不超過上限。
QUALITIES = (85, 75, 65, 55)

# Pillow 自己也有一道防線：超過這個數字會警告，超過兩倍直接拒絕。超過 MAX_PIXELS 的圖在
# 下面就會被擋掉、不會解開，所以這裡只當第二道防線，抓寬一點，正常的拒絕才不會在 log 裡留警告。
Image.MAX_IMAGE_PIXELS = MAX_PIXELS * 4


class ImageRejected(ValueError):
    """這張圖不收。code 給程式判斷，message 給使用者看。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def clean_jpeg(content: bytes, max_bytes: int) -> bytes:
    """把一張 JPEG 解開、轉正、只留畫素重新存檔，回傳新的檔案內容。"""
    try:
        with Image.open(BytesIO(content)) as opened:
            if opened.format != "JPEG":
                raise ImageRejected("image_not_jpeg", "附圖必須是 JPEG 圖片")
            width, height = opened.size
            if width * height > MAX_PIXELS or max(width, height) > MAX_SIDE:
                raise ImageRejected("image_too_large", "圖片的尺寸太大")
            # 有些相機拍直的照片時，存的是橫的畫面加上「請轉 90 度」的標記。
            # 標記等一下會跟著 EXIF 一起丟掉，所以先照標記把畫面轉正。
            upright = ImageOps.exif_transpose(opened).convert("RGB")
    except ImageRejected:
        raise
    except Image.DecompressionBombError:
        raise ImageRejected("image_too_large", "圖片的尺寸太大") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise ImageRejected("image_invalid", "這張圖片讀不出來") from None

    # 只留畫素：原本附帶的資訊（EXIF、註解、色彩描述檔）一律不帶過去。Pillow 存 JPEG 時
    # 會沿用圖上留著的註解，所以先清掉；EXIF 與色彩描述檔則是沒有明確傳進去就不會寫。
    upright.info.clear()
    for quality in QUALITIES:
        output = BytesIO()
        upright.save(output, format="JPEG", quality=quality, optimize=True)
        if output.tell() <= max_bytes:
            return output.getvalue()
    raise ImageRejected("image_too_large", "圖片太大")
