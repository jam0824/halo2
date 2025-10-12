// index.mjs
// npm i @openai/agents
import { Agent, run, MCPServerStdio, MCPServerStreamableHttp } from "@openai/agents";

// Tavily（検索/抽出/サイトマップ/クロール）— リモートHTTP/SSEで接続
const tavily = new MCPServerStreamableHttp({
  name: "tavily",
  url:
    process.env.TAVILY_MCP_URL // 例: 自前プロキシや mcp-remote を噛ませる場合
    || `https://mcp.tavily.com/mcp/?tavilyApiKey=${process.env.TAVILY_API_KEY}`,
  // 認証ヘッダで渡すことも可（クライアントが対応していれば）
  // requestInit: { headers: { Authorization: `Bearer ${process.env.TAVILY_API_KEY}` } },
});

// Playwright（ブラウザ操作）— ngrok の公開URLへ HTTP/SSE で接続
const playwright = new MCPServerStreamableHttp({
  name: "playwright",
  // ★ あなたの ngrok の https URL に置き換え（※Playwrightは /mcp がエンドポイント）
  url: process.env.PLAYWRIGHT_MCP_URL || "https://prevalid-unacrimoniously-leigh.ngrok-free.app/mcp",
  // 認証をかけている場合はヘッダも渡せます:
  // requestInit: { headers: { Authorization: "Bearer xxx" } },
});

// Spotify（オプション）— uv が未インストールでもスキップ可能に
const spotify = new MCPServerStdio({
  name: "spotify",
  fullCommand: "node ../spotify-mcp-server/build/index.js",
});

// switchbotで電気をオンオフするMCPサーバー
const switchbot = new MCPServerStdio({
  name: "switchbot", // ← エージェント側での識別名（自由）
  fullCommand: "python ./halo_mcp/switchbot.py",
  env: {
    // 念のためバッファ無効で安定化
    PYTHONUNBUFFERED: "1",
  },
});

async function connectSafe(server, name) {
  try {
    await server.connect();
    return server;
  } catch (e) {
    console.error(`[mcp] ${name} connect skipped: ${e?.message || e}`);
    return null;
  }
}

const listServers = [];
listServers.push(await connectSafe(switchbot, "switchbot"));
listServers.push(await connectSafe(tavily, "tavily"));
listServers.push(await connectSafe(spotify, "spotify"));
const activeServers = listServers.filter(Boolean);

try {
  const agent = new Agent({
    name: "multi-mcp-agent",
    model: "gpt-4o-mini",
    instructions: `
あなたはMCPツールを使ってユーザーの依頼を解決します。
- 高精度なWeb検索/要約/抽出/サイト構造化/クロール: 「tavily」
- 音楽の検索/再生/キュー/プレイリスト操作: 「spotify」
- 電気の操作: 「switchbot」
- 日本語で答える。
- 検索やクロール結果は分かりやすく簡潔な概要にまとめる。
- 出典やURLは削除して返信する。
- 結果からは改行を削除し、一行で返信する。
- あなたはガンダムのハロです。ハロ、電気をつけた。など片言で返信する。
- 一人称はハロです。`,
    mcpServers: activeServers,
  });

  const query =
    process.argv[2] ??
    "switchbotで電気をオン";
  const result = await run(agent, query);

  process.stdout.write(JSON.stringify({ output: result.finalOutput }) + "\n");
  await agent.close?.();
} catch (e) {
  console.error(e?.stack || String(e));
  process.exitCode = 1;
} finally {
  const listToClose = [];
  if (activeServers.includes(tavily)) listToClose.push(tavily.close());
  if (activeServers.includes(playwright)) listToClose.push(playwright.close());
  if (activeServers.includes(spotify)) listToClose.push(spotify.close());
  if (activeServers.includes(switchbot)) listToClose.push(switchbot.close());
  await Promise.allSettled(listToClose);
  process.exit(0);
}
