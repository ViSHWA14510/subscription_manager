import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    BOT_TOKEN: str      = os.getenv("BOT_TOKEN", "")
    OWNER_ID: int       = int(os.getenv("OWNER_ID", "0"))
    MONGO_URI: str      = os.getenv("MONGO_URI", "")
    MONGO_DB_NAME: str  = os.getenv("MONGO_DB_NAME", "telegram_sub_bot")
    UPI_ID: str         = os.getenv("UPI_ID", "")
    UPI_QR_FILE_ID: str = os.getenv("UPI_QR_URL", "")  # direct image URL of your QR code

    @classmethod
    def validate(cls):
        if not cls.BOT_TOKEN:
            raise ValueError("❌ BOT_TOKEN is not set in .env")
        if not cls.OWNER_ID:
            raise ValueError("❌ OWNER_ID is not set in .env")
        if not cls.MONGO_URI:
            raise ValueError("❌ MONGO_URI is not set in .env")
