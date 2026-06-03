import os
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict

from pymongo import MongoClient, ASCENDING
from pymongo.collection import Collection


MONGO_URI = os.getenv("MONGO_URI", "")
DB_NAME = os.getenv("MONGO_DB_NAME", "telegram_sub_bot")


class Database:
    def __init__(self):
        if not MONGO_URI:
            raise ValueError("❌ MONGO_URI is not set in environment variables")
        self.client = MongoClient(MONGO_URI)
        self.db = self.client[DB_NAME]
        self.channels: Collection = self.db["managed_channels"]
        self.subs: Collection = self.db["subscriptions"]

    # ─── Schema / Indexes ─────────────────────────────────

    def init_db(self):
        self.channels.create_index("channel_id", unique=True)
        self.subs.create_index(
            [("user_id", ASCENDING), ("channel_id", ASCENDING)], unique=True
        )
        self.subs.create_index("expiry_date")
        self.subs.create_index("is_active")
        print("✅ MongoDB initialized.")

    # ─── Managed Channels ─────────────────────────────────

    def add_managed_channel(self, channel_id: str, name: str, plan_months: int):
        self.channels.update_one(
            {"channel_id": channel_id},
            {"$set": {"channel_id": channel_id, "name": name, "plan_months": plan_months,
                      "created_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    def remove_managed_channel(self, channel_id: str):
        self.channels.delete_one({"channel_id": channel_id})

    def is_managed_channel(self, channel_id: str) -> bool:
        return self.channels.count_documents({"channel_id": channel_id}) > 0

    def get_managed_channels(self) -> List[Dict]:
        return list(self.channels.find({}, {"_id": 0}))

    def get_channel_plan(self, channel_id: str) -> int:
        doc = self.channels.find_one({"channel_id": channel_id}, {"plan_months": 1})
        return doc["plan_months"] if doc else 1

    def get_channel_info(self, channel_id: str) -> Optional[Dict]:
        doc = self.channels.find_one({"channel_id": channel_id}, {"_id": 0})
        return doc

    # ─── Subscriptions ────────────────────────────────────

    def add_subscription(self, user_id: int, channel_id: str, expiry: datetime, username: str = ""):
        self.subs.update_one(
            {"user_id": user_id, "channel_id": channel_id},
            {"$set": {
                "user_id": user_id,
                "channel_id": channel_id,
                "username": username,
                "expiry_date": expiry,
                "is_active": True,
                "notified_3d": False,
                "expired_notified": False,
                "created_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

    def update_subscription_expiry(self, user_id: int, channel_id: str, expiry: datetime):
        self.subs.update_one(
            {"user_id": user_id, "channel_id": channel_id},
            {"$set": {
                "expiry_date": expiry,
                "is_active": True,
                "notified_3d": False,
                "expired_notified": False,
            }},
        )

    def deactivate_subscription(self, user_id: int, channel_id: str):
        self.subs.update_one(
            {"user_id": user_id, "channel_id": channel_id},
            {"$set": {"is_active": False}},
        )

    def remove_subscription(self, user_id: int, channel_id: str):
        self.subs.delete_one({"user_id": user_id, "channel_id": channel_id})

    def get_subscription(self, user_id: int, channel_id: str) -> Optional[Dict]:
        doc = self.subs.find_one(
            {"user_id": user_id, "channel_id": channel_id}, {"_id": 0}
        )
        return self._attach_channel_name(doc) if doc else None

    def get_user_subscriptions(self, user_id: int) -> List[Dict]:
        docs = self.subs.find(
            {"user_id": user_id, "is_active": True},
            {"_id": 0},
        ).sort("expiry_date", ASCENDING)
        return [self._attach_channel_name(d) for d in docs]

    def get_all_subscriptions(self, channel_id: str = None) -> List[Dict]:
        query = {"is_active": True}
        if channel_id:
            query["channel_id"] = channel_id
        docs = self.subs.find(query, {"_id": 0}).sort("expiry_date", ASCENDING)
        return [self._attach_channel_name(d) for d in docs]

    def get_expiring_subscriptions(self, days: int = 3) -> List[Dict]:
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=days)
        docs = self.subs.find(
            {
                "is_active": True,
                "notified_3d": False,
                "expiry_date": {"$gt": now, "$lte": cutoff},
            },
            {"_id": 0},
        )
        return [self._attach_channel_name(d) for d in docs]

    def get_expired_subscriptions(self) -> List[Dict]:
        now = datetime.now(timezone.utc)
        docs = self.subs.find(
            {
                "is_active": True,
                "expired_notified": False,
                "expiry_date": {"$lte": now},
            },
            {"_id": 0},
        )
        return [self._attach_channel_name(d) for d in docs]

    def get_all_subscriptions_for_user(self, user_id: int) -> List[Dict]:
        """Return all subs (active + inactive) for a single user."""
        docs = self.subs.find({"user_id": user_id}, {"_id": 0}).sort("expiry_date", ASCENDING)
        return [self._attach_channel_name(d) for d in docs]

    def mark_notified(self, user_id: int, channel_id: str, level: str):
        if level == "3d":
            self.subs.update_one(
                {"user_id": user_id, "channel_id": channel_id},
                {"$set": {"notified_3d": True}},
            )

    def mark_expired_notified(self, user_id: int, channel_id: str):
        self.subs.update_one(
            {"user_id": user_id, "channel_id": channel_id},
            {"$set": {"expired_notified": True, "is_active": False}},
        )

    # ─── Internal helper ──────────────────────────────────

    def _attach_channel_name(self, doc: Dict) -> Dict:
        """Join channel name onto a subscription doc (replaces SQL LEFT JOIN)."""
        channel = self.channels.find_one(
            {"channel_id": doc.get("channel_id")}, {"name": 1, "_id": 0}
        )
        doc["channel_name"] = channel["name"] if channel else doc.get("channel_id", "")
        # Normalise expiry_date to isoformat string so callers stay unchanged
        if isinstance(doc.get("expiry_date"), datetime):
            doc["expiry_date"] = doc["expiry_date"].isoformat()
        return doc
