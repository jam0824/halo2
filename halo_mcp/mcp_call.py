# mcp_call.py
import subprocess, json, shutil, os, asyncio


class MCPClient:
    """Brave (MCP) エージェント呼び出し用クライアント。"""

    def __init__(self, node_path=None, script_path=None, extra_env=None):
        # node 実行ファイルの検出（未指定なら PATH から検索）
        self.node_path = node_path or shutil.which("node")
        if not self.node_path:
            raise RuntimeError("node が見つかりません。PATH を確認してください。")

        # index.mjs の場所（未指定ならこのファイルと同じディレクトリを基準に解決）
        if script_path is None:
            base_dir = os.path.dirname(__file__)
            self.script_path = os.path.join(base_dir, "index.mjs")
        else:
            self.script_path = script_path

        # カラーコード抑制などの環境変数を設定
        base_env = {**os.environ, "NODE_DISABLE_COLORS": "1", "FORCE_COLOR": "0"}
        if extra_env:
            base_env.update(extra_env)
        self.env = base_env

    async def call(self, query: str) -> str:
        # Nodeプロセスを非同期で起動し、stdout/stderr を受け取る
        proc = await asyncio.create_subprocess_exec(
            self.node_path,
            self.script_path,
            query,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
        )

        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout = (stdout_bytes or b"").decode("utf-8", errors="replace").strip()
        stderr = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            raise RuntimeError(f"Node script failed (code {proc.returncode}).\nSTDERR:\n{stderr}")

        # もし他のログが混ざる可能性がある場合に備え、先頭/末尾のJSON行を拾う簡易ガード
        # ここでは素直に1行JSONを想定（index.mjs側がJSONのみをconsole.logする前提）。
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            # 万一混ざったら、波括弧の最初と最後を抜く簡易フォールバック
            import re
            m = re.search(r"\{.*\}", stdout, flags=re.S)
            if not m:
                raise RuntimeError(f"JSONの解析に失敗しました。STDOUT:\n{stdout}\n\nSTDERR:\n{stderr}")
            data = json.loads(m.group(0))

        return data["output"]


def call_brave_agent(query: str) -> str:
    """後方互換の同期ラッパー。内部で MCPClient.call を実行。"""
    client = MCPClient()
    return asyncio.run(client.call(query))

if __name__ == "__main__":
    client = MCPClient()
    # ans = asyncio.run(client.call("https://news.ycombinator.com にアクセスして、'new' を開いてタイトルを3件教えて。そのあと30秒待って"))
    # ans = asyncio.run(client.call("超魔界村について調べて"))
    ans = asyncio.run(client.call("tavilyでぼっちちゃんについて調べて"))
    print("=== Python received ===")
    print(ans)
