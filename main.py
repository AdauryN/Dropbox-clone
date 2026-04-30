import hashlib
import importlib.util
import os

import io
from flask import Flask, request, jsonify, send_file
import firebase_admin
from firebase_admin import credentials, auth, firestore as fs
from google.cloud import storage as gcs
from google.cloud.firestore import ArrayUnion

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
storage_client = gcs.Client(project=local_constants.PROJECT_ID)
bucket = storage_client.bucket(local_constants.STORAGE_BUCKET)

# Auth helper

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


# Static entry point

@app.route("/")
def index():
    return app.send_static_file("index.html")


# Login — creates User doc + root directory on first sign-in

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


# Directories

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

    # no duplicate names in the same location
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

    # block deletion of non-empty directories
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


# Files

@app.route("/api/files", methods=["GET"])
def list_files():
    uid, err = _require_uid()
    if err:
        return err

    directory_path = request.args.get("path", "/")
    docs = (
        db.collection("files")
        .where("owner", "==", uid)
        .where("directory_path", "==", directory_path)
        .stream()
    )
    return jsonify([
        {"id": d.id, "name": d.to_dict()["name"], "size": d.to_dict().get("size", 0)}
        for d in docs
    ])


@app.route("/api/files", methods=["POST"])
def upload_file():
    uid, err = _require_uid()
    if err:
        return err

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    uploaded = request.files["file"]
    directory_path = request.form.get("directory_path", "/")
    overwrite = request.form.get("overwrite", "false").lower() == "true"
    filename = uploaded.filename

    if not filename:
        return jsonify({"error": "Filename is missing"}), 400

    file_bytes = uploaded.read()
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    size = len(file_bytes)

    # Check if a file with this name already exists in the directory
    existing = (
        db.collection("files")
        .where("owner", "==", uid)
        .where("directory_path", "==", directory_path)
        .where("name", "==", filename)
        .limit(1)
        .get()
    )

    # ask before overwriting
    if existing and not overwrite:
        return jsonify({"error": "File already exists", "exists": True}), 409

    # Cross-directory hash duplicate detection
    duplicate_confirmed = request.form.get("duplicate_confirmed", "false").lower() == "true"
    if not overwrite and not duplicate_confirmed:
        hash_match = (
            db.collection("files")
            .where("owner", "==", uid)
            .where("hash", "==", file_hash)
            .limit(1)
            .get()
        )
        if hash_match:
            dupe = hash_match[0].to_dict()
            return jsonify({
                "error": "Identical file already exists",
                "duplicate": True,
                "existing_path": dupe["directory_path"],
                "existing_name": dupe["name"],
            }), 409

    # Build GCS object path: uid/path/to/dir/filename
    dir_suffix = directory_path.strip("/")
    gcs_path = f"{uid}/{dir_suffix}/{filename}" if dir_suffix else f"{uid}/{filename}"

    blob = bucket.blob(gcs_path)
    blob.upload_from_string(
        file_bytes,
        content_type=uploaded.content_type or "application/octet-stream",
    )

    if existing and overwrite:
        existing[0].reference.update({
            "gcs_path": gcs_path,
            "size": size,
            "hash": file_hash,
            "updated_at": fs.SERVER_TIMESTAMP,
        })
    else:
        db.collection("files").add({
            "name": filename,
            "owner": uid,
            "directory_path": directory_path,
            "gcs_path": gcs_path,
            "size": size,
            "hash": file_hash,
            "created_at": fs.SERVER_TIMESTAMP,
        })

    return jsonify({"status": "uploaded"}), 201


@app.route("/api/files/<file_id>", methods=["DELETE"])
def delete_file(file_id):
    uid, err = _require_uid()
    if err:
        return err

    file_ref = db.collection("files").document(file_id)
    file_doc = file_ref.get()

    if not file_doc.exists:
        return jsonify({"error": "File not found"}), 404

    file_data = file_doc.to_dict()
    if file_data["owner"] != uid:
        return jsonify({"error": "Forbidden"}), 403

    # Delete from GCS then Firestore
    bucket.blob(file_data["gcs_path"]).delete()
    file_ref.delete()
    return jsonify({"status": "deleted"})


@app.route("/api/files/<file_id>/download", methods=["GET"])
def download_file(file_id):
    uid, err = _require_uid()
    if err:
        return err

    file_ref = db.collection("files").document(file_id)
    file_doc = file_ref.get()

    if not file_doc.exists:
        return jsonify({"error": "File not found"}), 404

    file_data = file_doc.to_dict()
    if file_data["owner"] != uid and uid not in file_data.get("shared_with", []):
        return jsonify({"error": "Forbidden"}), 403

    file_bytes = bucket.blob(file_data["gcs_path"]).download_as_bytes()
    return send_file(
        io.BytesIO(file_bytes),
        download_name=file_data["name"],
        as_attachment=True,
    )

# File sharing

@app.route("/api/files/<file_id>/share", methods=["POST"])
def share_file(file_id):
    uid, err = _require_uid()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    target_email = data.get("email", "").strip().lower()
    if not target_email:
        return jsonify({"error": "Email is required"}), 400

    file_ref = db.collection("files").document(file_id)
    file_doc = file_ref.get()
    if not file_doc.exists:
        return jsonify({"error": "File not found"}), 404
    if file_doc.to_dict()["owner"] != uid:
        return jsonify({"error": "Forbidden"}), 403

    target_users = db.collection("users").where("email", "==", target_email).limit(1).get()
    if not target_users:
        return jsonify({"error": "No account found for that email"}), 404

    target_uid = target_users[0].to_dict()["uid"]
    if target_uid == uid:
        return jsonify({"error": "Cannot share with yourself"}), 400

    file_ref.update({"shared_with": ArrayUnion([target_uid])})
    return jsonify({"status": "shared"})


@app.route("/api/shared", methods=["GET"])
def list_shared_files():
    uid, err = _require_uid()
    if err:
        return err

    docs = (
        db.collection("files")
        .where("shared_with", "array_contains", uid)
        .stream()
    )
    return jsonify([
        {
            "id": d.id,
            "name": d.to_dict()["name"],
            "size": d.to_dict().get("size", 0),
            "directory_path": d.to_dict()["directory_path"],
        }
        for d in docs
    ])


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, host="0.0.0.0", port=8080)
