import http.server
import socketserver
import webbrowser
import os
import sys

PORT = 5173
DIRECTORY = os.path.dirname(os.path.abspath(__file__))

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        # Disable browser caching for development
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

def run():
    os.chdir(DIRECTORY)
    # Enable address reuse
    socketserver.TCPServer.allow_reuse_address = True
    
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        url = f"http://localhost:{PORT}"
        print("=" * 60)
        print(f"🚀 MyToDoList 本地伺服器已啟動！")
        print(f"🌐 瀏覽器網址: {url}")
        print(f"📁 專案路徑:   {DIRECTORY}")
        print(f"🔥 Firebase 專案: my-to-do-list-ac553")
        print("=" * 60)
        print("按 Ctrl + C 可停止伺服器\n")
        
        try:
            webbrowser.open(url)
        except Exception:
            pass
            
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n伺服器已正常關閉。")
            sys.exit(0)

if __name__ == "__main__":
    run()
