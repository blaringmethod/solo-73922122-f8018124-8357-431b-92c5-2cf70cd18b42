"""离线档案断链核查 —— Flask 应用工厂。"""
import os

from flask import Flask


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-secret"),
        DATABASE=os.environ.get("ARCHIVE_DB")
        or os.path.join(app.instance_path, "archive.db"),
        # 仅供测试/演示：核查遍历每处理一页 sleep 多少毫秒（便于复现交错操作）
        CHECK_STEP_DELAY_MS=int(os.environ.get("CHECK_STEP_DELAY_MS", "0")),
        MAX_CONTENT_LENGTH=512 * 1024 * 1024,  # 允许大型目录 CSV
    )
    if test_config:
        app.config.update(test_config)

    db_dir = os.path.dirname(app.config["DATABASE"])
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    from . import db

    db.init_app(app)

    from . import views

    app.register_blueprint(views.bp)
    return app
