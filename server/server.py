import asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pathlib import Path
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
from fake_memory.search_diary import search_diaries


app = FastAPI(title="Halo Server")


class RunRequest(BaseModel):
    message: str


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(content={"status": "ok"}, media_type="application/json; charset=utf-8")


# ヘルパー: 指定日のダイアリーファイルを読み込む（ファイルオープン処理を分離）
async def read_diary_content(date_str: str) -> str:
    base_dir = Path(__file__).resolve().parent.parent
    file_path = base_dir / "fake_memory/diary" / f"{date_str}_diary.txt"
    if not file_path.exists():
        raise FileNotFoundError(f"Diary file not found: {file_path}")
    # 同期ファイルI/Oをスレッドに逃がして非同期対応
    return await asyncio.to_thread(file_path.read_text, encoding="utf-8")


# ヘルパー: 指定日のサマリーファイルを読み込む（ファイルオープン処理を分離）
async def read_summary_content(date_str: str) -> str:
    base_dir = Path(__file__).resolve().parent.parent
    file_path = base_dir / "fake_memory/diary" / f"{date_str}_summary.txt"
    if not file_path.exists():
        raise FileNotFoundError(f"Summary file not found: {file_path}")
    return await asyncio.to_thread(file_path.read_text, encoding="utf-8")


# 直近n日分のダイアリーを結合して返す（静的ルートを先に定義して、動的ルートより優先）
@app.get("/diary/recent")
async def get_recent_diary(days: int = 1) -> JSONResponse:
    try:
        if days <= 0:
            days = 1
        # 暴走防止の上限
        if days > 30:
            days = 30
        listDates: list[str] = []
        listContents: list[str] = []
        today_dt = datetime.now()
        for i in range(days):
            date_str = (today_dt - timedelta(days=i)).strftime("%Y%m%d")
            try:
                content = await read_diary_content(date_str)
                listDates.append(date_str)
                listContents.append(content)
            except FileNotFoundError:
                # 無い日はスキップ
                continue
        return JSONResponse(
            content={"dates": listDates, "content": "\n".join(listContents)},
            media_type="application/json; charset=utf-8",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# エンドポイント: 指定日のサマリー（例: 20250920_summary.txt）を返す
@app.get("/diary/summary/{date_int}")
async def get_summary(date_int: int) -> JSONResponse:
    try:
        date_str = f"{date_int:08d}"
        content = await read_summary_content(date_str)
        return JSONResponse(content={"date": date_str, "content": content}, media_type="application/json; charset=utf-8")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# エンドポイント: キーワードで日記を検索する
@app.get("/diary/search")
async def search_diary_by_keyword(keyword: str) -> JSONResponse:
    try:
        if not keyword:
            raise HTTPException(status_code=400, detail="Keyword cannot be empty")
        # 同期処理をスレッドに逃がして非同期対応
        results = await asyncio.to_thread(search_diaries, keyword)
        return JSONResponse(
            content={
                "keyword": keyword,
                "matches": results,
                "count": len(results),
            },
            media_type="application/json; charset=utf-8",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# エンドポイント: 整数日付（例: 20250920）で指定し、内容を返す
@app.get("/diary/{date_int}")
async def get_diary(date_int: int) -> JSONResponse:
    try:
        date_str = f"{date_int:08d}"
        content = await read_diary_content(date_str)
        return JSONResponse(content={"date": date_str, "content": content}, media_type="application/json; charset=utf-8")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=50022)


