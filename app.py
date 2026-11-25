
import os
from datetime import datetime, timezone
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, session
from firebase_admin import firestore
from google.cloud.firestore_v1 import SERVER_TIMESTAMP
from firebase_config import db

load_dotenv()

# ==========================================
# FLASK CONFIGURATION - CSS AND JS CONNECTION
# ==========================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
    static_url_path="/static"
)

app.secret_key = os.getenv(
    "SECRET_KEY",
    "change-this-development-secret"
)


# ==========================================
# FIRESTORE COLLECTIONS
# ==========================================

COLLECTIONS = {
    "customers": {
        "required": ["full_name", "phone_number", "complete_address"],
        "fields": [
            "full_name", "phone_number", "email",
            "complete_address", "account_status"
        ],
        "search": ["full_name", "phone_number", "email"],
    },

    "products": {
        "required": ["product_name", "container_size", "price"],
        "fields": [
            "product_name", "container_size", "price",
            "stock_quantity", "availability", "description"
        ],
        "search": ["product_name", "container_size"],
    },

    "orders": {
        "required": ["customer_id", "order_type", "total_amount"],
        "fields": [
            "customer_id", "customer_name", "order_date",
            "order_type", "total_amount", "payment_status",
            "order_status", "delivery_address"
        ],
        "search": ["customer_name", "order_id", "order_status"],
    },

    "order_details": {
        "required": ["order_id", "product_id", "quantity", "unit_price"],
        "fields": [
            "order_id", "product_id", "product_name",
            "container_size", "quantity", "unit_price", "subtotal"
        ],
        "search": ["order_id", "product_name"],
    },

    "deliveries": {
        "required": ["order_id", "delivery_address"],
        "fields": [
            "order_id", "customer_id", "customer_name",
            "delivery_person", "delivery_address",
            "scheduled_date", "delivery_status", "delivery_notes"
        ],
        "search": [
            "order_id", "customer_name",
            "delivery_person", "delivery_status"
        ],
    },

    "containers": {
        "required": ["customer_id", "container_type", "quantity_released"],
        "fields": [
            "customer_id", "customer_name", "order_id",
            "container_type", "quantity_released",
            "quantity_returned", "outstanding_quantity",
            "return_status", "transaction_date"
        ],
        "search": [
            "customer_name", "container_type", "return_status"
        ],
    },

    "payments": {
        "required": [
            "customer_id", "order_id",
            "amount_paid", "payment_method"
        ],
        "fields": [
            "customer_id", "customer_name", "order_id",
            "amount_paid", "payment_method", "payment_date",
            "payment_status", "reference_number"
        ],
        "search": [
            "customer_name", "order_id",
            "payment_method", "reference_number"
        ],
    },
}


# ==========================================
# HELPER FUNCTIONS
# ==========================================

def clean_value(value):
    if isinstance(value, str):
        return value.strip()
    return value


def serialize(doc):
    data = doc.to_dict() or {}
    data["id"] = doc.id

    for key, value in list(data.items()):
        if hasattr(value, "isoformat"):
            data[key] = value.isoformat()

    return data


def error(message, status=400):
    return jsonify({
        "success": False,
        "error": message
    }), status


# ==========================================
# DATA VALIDATION
# ==========================================

def validate_payload(collection, payload, partial=False):

    schema = COLLECTIONS[collection]

    if not isinstance(payload, dict):
        return "Request body must be a JSON object."

    if not partial:
        missing = [
            f for f in schema["required"]
            if payload.get(f) in (None, "")
        ]

        if missing:
            return "Required fields missing: " + ", ".join(missing)

    for field in schema["fields"]:
        if field in payload:
            payload[field] = clean_value(payload[field])

    numeric_fields = {
        "products": ["price", "stock_quantity"],
        "orders": ["total_amount"],
        "order_details": ["quantity", "unit_price", "subtotal"],
        "containers": [
            "quantity_released",
            "quantity_returned",
            "outstanding_quantity"
        ],
        "payments": ["amount_paid"],
    }.get(collection, [])

    for field in numeric_fields:

        if field in payload and payload[field] not in (None, ""):

            try:
                if field in (
                    "price",
                    "total_amount",
                    "unit_price",
                    "subtotal",
                    "amount_paid"
                ):
                    payload[field] = float(payload[field])
                else:
                    payload[field] = int(payload[field])

            except (ValueError, TypeError):
                return f"{field} must be a valid number."

            if payload[field] < 0:
                return f"{field} cannot be negative."

    if collection == "containers":

        released = int(payload.get("quantity_released", 0))
        returned = int(payload.get("quantity_returned", 0))

        if returned > released:
            return "Quantity returned cannot exceed quantity released."

        payload["outstanding_quantity"] = released - returned

        payload["return_status"] = (
            "Returned" if released == returned
            else (
                "Partially Returned" if returned
                else "Not Returned"
            )
        )

    return None


# ==========================================
# REFERENCE VALIDATION
# ==========================================

def verify_references(collection, payload):

    checks = {
        "orders": [
            ("customer_id", "customers")
        ],

        "order_details": [
            ("order_id", "orders"),
            ("product_id", "products")
        ],

        "deliveries": [
            ("order_id", "orders")
        ],

        "containers": [
            ("customer_id", "customers")
        ],

        "payments": [
            ("customer_id", "customers"),
            ("order_id", "orders")
        ],
    }

    for field, target in checks.get(collection, []):

        value = payload.get(field)

        if value and not db.collection(target).document(
            str(value)
        ).get().exists:

            return (
                f"Referenced {target[:-1]} record "
                f"'{value}' does not exist."
            )

    return None


# ==========================================
# HOME PAGE
# ==========================================

@app.get("/")
def index():
    return render_template("index.html")


# ==========================================
# ADMIN LOGIN
# ==========================================

@app.post("/api/login")
def login():

    data = request.get_json(silent=True) or {}

    username = os.getenv("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD", "admin123")

    if (
        data.get("username") == username
        and data.get("password") == password
    ):

        session["user"] = {
            "username": username,
            "role": "Administrator"
        }

        return jsonify({
            "success": True,
            "user": session["user"]
        })

    return error("Invalid username or password.", 401)


@app.post("/api/logout")
def logout():

    session.clear()

    return jsonify({
        "success": True
    })


@app.get("/api/session")
def get_session():

    return jsonify({
        "authenticated": bool(session.get("user")),
        "user": session.get("user")
    })


# ==========================================
# CREATE AND READ COLLECTION RECORDS
# ==========================================

@app.route("/api/<collection>", methods=["GET", "POST"])
def collection_endpoint(collection):

    if collection not in COLLECTIONS:
        return error("Unknown collection.", 404)

    # READ RECORDS
    if request.method == "GET":

        try:

            docs = db.collection(collection).stream()

            records = [serialize(d) for d in docs]

            q = (
                request.args.get("q") or ""
            ).lower().strip()

            if q:

                searchable = COLLECTIONS[collection]["search"]

                records = [
                    r for r in records
                    if any(
                        q in str(r.get(k, "")).lower()
                        for k in searchable
                    )
                ]

            return jsonify({
                "success": True,
                "data": records,
                "count": len(records)
            })

        except Exception as exc:

            app.logger.exception("Firestore read failed")

            return error(
                "Could not retrieve records: " + str(exc),
                500
            )

    # CREATE RECORDS
    payload = request.get_json(silent=True) or {}

    validation_error = validate_payload(collection, payload)

    if validation_error:
        return error(validation_error)

    reference_error = verify_references(collection, payload)

    if reference_error:
        return error(reference_error)

    try:

        now = datetime.now(timezone.utc).isoformat()

        payload["created_at"] = now
        payload["updated_at"] = now

        if collection == "orders" and not payload.get("order_status"):
            payload["order_status"] = "Pending"

        if collection == "orders" and not payload.get("payment_status"):
            payload["payment_status"] = "Unpaid"

        if collection == "products" and not payload.get("availability"):
            payload["availability"] = "Available"

        if collection == "containers":

            payload.setdefault("quantity_returned", 0)

            payload["outstanding_quantity"] = (
                int(payload["quantity_released"])
                - int(payload["quantity_returned"])
            )

        ref = db.collection(collection).document()

        payload["record_id"] = ref.id

        ref.set(payload)

        return jsonify({
            "success": True,
            "message": "Record created successfully.",
            "data": {
                "id": ref.id,
                **payload
            }
        }), 201

    except Exception as exc:

        app.logger.exception("Firestore create failed")

        return error(
            "Could not create record: " + str(exc),
            500
        )


# ==========================================
# GET, UPDATE AND DELETE INDIVIDUAL RECORDS
# ==========================================

@app.route(
    "/api/<collection>/<record_id>",
    methods=["GET", "PUT", "DELETE"]
)
def record_endpoint(collection, record_id):

    if collection not in COLLECTIONS:
        return error("Unknown collection.", 404)

    ref = db.collection(collection).document(record_id)

    try:

        snapshot = ref.get()

        if not snapshot.exists:
            return error("Record not found.", 404)

        # GET RECORD
        if request.method == "GET":

            return jsonify({
                "success": True,
                "data": serialize(snapshot)
            })

        # DELETE RECORD
        if request.method == "DELETE":

            ref.delete()

            return jsonify({
                "success": True,
                "message": "Record deleted successfully."
            })

        # UPDATE RECORD
        payload = request.get_json(silent=True) or {}

        validation_error = validate_payload(
            collection,
            payload,
            partial=True
        )

        if validation_error:
            return error(validation_error)

        reference_error = verify_references(
            collection,
            payload
        )

        if reference_error:
            return error(reference_error)

        payload["updated_at"] = datetime.now(
            timezone.utc
        ).isoformat()

        ref.update(payload)

        updated = ref.get()

        return jsonify({
            "success": True,
            "message": "Record updated successfully.",
            "data": serialize(updated)
        })

    except Exception as exc:

        app.logger.exception("Firestore operation failed")

        return error(
            "Database operation failed: " + str(exc),
            500
        )


# ==========================================
# DASHBOARD STATISTICS
# ==========================================

@app.get("/api/dashboard")
def dashboard():

    try:

        counts = {}

        for name in COLLECTIONS:

            counts[name] = len(
                list(db.collection(name).stream())
            )

        orders = [
            serialize(d)
            for d in db.collection("orders").stream()
        ]

        payments = [
            serialize(d)
            for d in db.collection("payments").stream()
        ]

        sales = sum(
            float(p.get("amount_paid", 0) or 0)
            for p in payments
        )

        pending = sum(
            1 for o in orders
            if o.get("order_status") not in (
                "Completed",
                "Cancelled"
            )
        )

        return jsonify({
            "success": True,
            "counts": counts,
            "sales": sales,
            "pending_orders": pending
        })

    except Exception as exc:

        return error(
            "Could not load dashboard: " + str(exc),
            500
        )


# ==========================================
# REPORTS
# ==========================================

@app.get("/api/reports/<report_type>")
def reports(report_type):

    mapping = {
        "sales": "payments",
        "orders": "orders",
        "deliveries": "deliveries",
        "payments": "payments",
    }

    if report_type not in mapping:
        return error("Unknown report.", 404)

    try:

        records = [
            serialize(d)
            for d in db.collection(
                mapping[report_type]
            ).stream()
        ]

        return jsonify({
            "success": True,
            "data": records,
            "count": len(records)
        })

    except Exception as exc:

        return error(
            "Could not load report: " + str(exc),
            500
        )


# ==========================================
# API HEALTH CHECK
# ==========================================

@app.get("/api/health")
def health():

    return jsonify({
        "success": True,
        "message": "Water Refill Station API is running."
    })


# ==========================================
# RUN APPLICATION
# ==========================================

if __name__ == "__main__":

    app.run(
        debug=os.getenv("FLASK_DEBUG", "true").lower() == "true"
    )
