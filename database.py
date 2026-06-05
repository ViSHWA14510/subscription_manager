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
        self.pending: Collection = self.db["pending_approvals"]
        self.payment_requests: Collection = self.db["payment_requests"]

    # ─── Schema / Indexes ─────────────────────────────────

    def init_db(self):
        self.channels.create_index("channel_id", unique=True)
        self.subs.create_index(
            [("user_id", ASCENDING), ("channel_id", ASCENDING)], unique=True
        )
        self.subs.create_index("expiry_date")
        self.subs.create_index("is_active")
        self.pending.create_index(
            [("user_id", ASCENDING), ("channel_id", ASCENDING)], unique=True
        )
        self.payment_requests.create_index(
            [("user_id", ASCENDING), ("channel_id", ASCENDING)]
        )
        self.payment_requests.create_index("status")
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

    # ─── Payment Requests ─────────────────────────────────

    def add_payment_request(self, user_id: int, channel_id: str, channel_name: str,
                             username: str, first_name: str, months: int,
                             amount: int, screenshot_file_id: str = None):
        import uuid
        request_id = str(uuid.uuid4())[:8].upper()
        self.payment_requests.insert_one({
            "request_id": request_id,
            "user_id": user_id,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "username": username,
            "first_name": first_name,
            "months": months,
            "amount": amount,
            "screenshot_file_id": screenshot_file_id,
            "status": "pending",   # pending | approved | rejected
            "created_at": datetime.now(timezone.utc),
        })
        return request_id

    def get_payment_request(self, request_id: str) -> Optional[Dict]:
        doc = self.payment_requests.find_one({"request_id": request_id}, {"_id": 0})
        return doc

    def get_pending_payment_requests(self) -> List[Dict]:
        return list(self.payment_requests.find(
            {"status": "pending"}, {"_id": 0}
        ).sort("created_at", ASCENDING))

    def update_payment_status(self, request_id: str, status: str):
        self.payment_requests.update_one(
            {"request_id": request_id},
            {"$set": {"status": status, "resolved_at": datetime.now(timezone.utc)}}
        )

    def get_user_pending_payment(self, user_id: int, channel_id: str) -> Optional[Dict]:
        return self.payment_requests.find_one(
            {"user_id": user_id, "channel_id": channel_id, "status": "pending"},
            {"_id": 0}
        )

    # ─── Pending Approvals (join-based) ──────────────────

    def add_pending(self, user_id: int, channel_id: str, username: str,
                    first_name: str, join_date: datetime, channel_name: str):
        self.pending.update_one(
            {"user_id": user_id, "channel_id": channel_id},
            {"$set": {
                "user_id": user_id,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "username": username,
                "first_name": first_name,
                "join_date": join_date,
                "created_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

    def get_pending(self, user_id: int, channel_id: str) -> Optional[Dict]:
        doc = self.pending.find_one(
            {"user_id": user_id, "channel_id": channel_id}, {"_id": 0}
        )
        if doc and isinstance(doc.get("join_date"), datetime):
            doc["join_date"] = doc["join_date"].isoformat()
        return doc

    def remove_pending(self, user_id: int, channel_id: str):
        self.pending.delete_one({"user_id": user_id, "channel_id": channel_id})

    def get_all_pending(self) -> List[Dict]:
        docs = list(self.pending.find({}, {"_id": 0}).sort("created_at", ASCENDING))
        for doc in docs:
            if isinstance(doc.get("join_date"), datetime):
                doc["join_date"] = doc["join_date"].isoformat()
        return docs

    # ─── Subscriptions ────────────────────────────────────

    def add_subscription(self, user_id: int, channel_id: str, expiry: datetime,
                         username: str = "", join_date: datetime = None,
                         months: int = None, channel_name: str = None):
        fields = {
            "user_id": user_id,
            "channel_id": channel_id,
            "username": username,
            "expiry_date": expiry,
            "is_active": True,
            "notified_3d": False,
            "notified_1d": False,
            "expired_notified": False,
            "created_at": join_date or datetime.now(timezone.utc),
            "join_date": join_date or datetime.now(timezone.utc),
        }
        if months is not None:
            fields["months"] = months
        if channel_name:
            fields["channel_name"] = channel_name
        self.subs.update_one(
            {"user_id": user_id, "channel_id": channel_id},
            {"$set": fields},
            upsert=True,
        )

    def update_subscription_expiry(self, user_id: int, channel_id: str,
                                   expiry: datetime, join_date: datetime = None):
        update_fields = {
            "expiry_date": expiry,
            "is_active": True,
            "notified_3d": False,
            "notified_1d": False,
            "expired_notified": False,
        }
        if join_date:
            update_fields["join_date"] = join_date
            update_fields["created_at"] = join_date
        self.subs.update_one(
            {"user_id": user_id, "channel_id": channel_id},
            {"$set": update_fields},
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

    def get_expiring_subscriptions_1d(self) -> List[Dict]:
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=1)
        docs = self.subs.find(
            {
                "is_active": True,
                "notified_1d": False,
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
        elif level == "1d":
            self.subs.update_one(
                {"user_id": user_id, "channel_id": channel_id},
                {"$set": {"notified_1d": True}},
            )

    def set_join_date(self, user_id: int, channel_id: str, join_date: datetime):
        """Manually set or correct a user's join date."""
        self.subs.update_one(
            {"user_id": user_id, "channel_id": channel_id},
            {"$set": {"join_date": join_date, "created_at": join_date}},
        )

    def mark_expired_notified(self, user_id: int, channel_id: str):
        self.subs.update_one(
            {"user_id": user_id, "channel_id": channel_id},
            {"$set": {"expired_notified": True, "is_active": False}},
        )

    # ─── Internal helper ──────────────────────────────────

    def _attach_channel_name(self, doc: Dict) -> Dict:
        """Join channel name onto a subscription doc."""
        channel = self.channels.find_one(
            {"channel_id": doc.get("channel_id")}, {"name": 1, "_id": 0}
        )
        doc["channel_name"] = channel["name"] if channel else doc.get("channel_id", "")
        if isinstance(doc.get("expiry_date"), datetime):
            doc["expiry_date"] = doc["expiry_date"].isoformat()
        if isinstance(doc.get("join_date"), datetime):
            doc["join_date"] = doc["join_date"].isoformat()
        return doc
