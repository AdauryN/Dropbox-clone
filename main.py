import importlib.util
import os

from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, auth, firestore as fs

# local-constants.py uses a hyphen so standard import won't work
_spec = importlib.util.spec_from_file_location(
    "local_constants",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "local-constants.py"),
)
local_constants = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(local_constants)

app = Flask(__name__, static_folder="static")

firebase_admin.initialize_app(
    credentials.ApplicationDefault(),
    {"projectId": local_constants.PROJECT_ID},
)
db = fs.client()


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _require_uid():
    """Verify the Bearer token and return (uid, None) or (None, error response)."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, (jsonify({"error": "Unauthorized"}), 401)
    try:
        uid = auth.verify_id_token(header.split(" ", 1)[1])["uid"]
        return uid, None
    except Exception:
        return None, (jsonify({"error": "Invalid token"}), 401)


# ---------------------------------------------------------------------------
# Static entry point
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return app.send_static_file("index.html")


# ---------------------------------------------------------------------------
# Login — creates User doc + root directory on first sign-in
# ---------------------------------------------------------------------------

@app.route("/api/login", methods=["POST"])
def login():
    uid, err = _require_uid()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    user_ref = db.collection("users").document(uid)

    if not user_ref.get().exists:
        user_ref.set({
            "uid": uid,
            "email": data.get("email", ""),
            "created_at": fs.SERVER_TIMESTAMP,
        })
        # Root directory — path "/" with no parent
        db.collection("directories").add({
            "name": "root",
            "owner": uid,
            "path": "/",
            "parent_path": None,
            "created_at": fs.SERVER_TIMESTAMP,
        })

    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------

@app.route("/api/directories", methods=["GET"])
def list_directories():
    uid, err = _require_uid()
    if err:
        return err

    parent_path = request.args.get("path", "/")
    docs = (
        db.collection("directories")
        .where("owner", "==", uid)
        .where("parent_path", "==", parent_path)
        .stream()
    )
    return jsonify([
        {"id": d.id, "name": d.to_dict()["name"], "path": d.to_dict()["path"]}
        for d in docs
    ])


@app.route("/api/directories", methods=["POST"])
def create_directory():
    uid, err = _require_uid()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    parent_path = data.get("parent_path", "/")

    if not name:
        return jsonify({"error": "Directory name is required"}), 400

    # Critical rule: no duplicate names in the same location
    dupe = (
        db.collection("directories")
        .where("owner", "==", uid)
        .where("parent_path", "==", parent_path)
        .where("name", "==", name)
        .limit(1)
        .get()
    )
    if dupe:
        return jsonify({"error": "A directory with that name already exists here"}), 409

    new_path = f"/{name}" if parent_path == "/" else f"{parent_path}/{name}"
    db.collection("directories").add({
        "name": name,
        "owner": uid,
        "path": new_path,
        "parent_path": parent_path,
        "created_at": fs.SERVER_TIMESTAMP,
    })
    return jsonify({"status": "created", "path": new_path}), 201


@app.route("/api/directories/<dir_id>", methods=["DELETE"])
def delete_directory(dir_id):
    uid, err = _require_uid()
    if err:
        return err

    dir_ref = db.collection("directories").document(dir_id)
    dir_doc = dir_ref.get()

    if not dir_doc.exists:
        return jsonify({"error": "Directory not found"}), 404

    dir_data = dir_doc.to_dict()
    if dir_data["owner"] != uid:
        return jsonify({"error": "Forbidden"}), 403

    dir_path = dir_data["path"]

    # Critical rule: block deletion of non-empty directories
    if (
        db.collection("directories")
        .where("owner", "==", uid)
        .where("parent_path", "==", dir_path)
        .limit(1)
        .get()
    ):
        return jsonify({"error": "Directory contains subdirectories and cannot be deleted"}), 400

    if (
        db.collection("files")
        .where("owner", "==", uid)
        .where("directory_path", "==", dir_path)
        .limit(1)
        .get()
    ):
        return jsonify({"error": "Directory contains files and cannot be deleted"}), 400

    dir_ref.delete()
    return jsonify({"status": "deleted"})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8080)
