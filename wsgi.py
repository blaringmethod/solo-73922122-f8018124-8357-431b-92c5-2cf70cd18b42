from app import create_app

app = create_app()

if __name__ == "__main__":
    # 本地开发直接运行：python wsgi.py
    app.run(host="0.0.0.0", port=8000, threaded=True)
