def test_mindmap_png_renderer_preserves_outline_and_unicode():
    from PIL import Image

    from localplaud.export_mindmap import parse_outline, render_mindmap_png

    markdown = "# 會議重點\n- 決策\n  - 下一步 action\n- 風險"
    outline = parse_outline(markdown)
    assert [(item.depth, item.text) for item in outline] == [
        (0, "會議重點"), (1, "決策"), (2, "下一步 action"), (1, "風險")
    ]
    png = render_mindmap_png(markdown, title="Weekly 會議")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    import io

    image = Image.open(io.BytesIO(png))
    assert image.width == 1600
    assert image.height > 260


def test_mindmap_png_endpoint_and_stale_guard(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'map.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.models import PlaudFile, StageName, StageRun, StageStatus, Summary
    from localplaud.db.session import init_db, session_scope

    init_db()
    with session_scope() as session:
        session.add(PlaudFile(id="map", filename="Map"))
        session.add(Summary(file_id="map", template="mind_map", source="local",
                            title="Map", content_md="# Root\n- Child"))
        session.add(StageRun(file_id="map", stage=StageName.mind_map,
                             status=StageStatus.completed, detail={}))
    client = TestClient(app)
    response = client.get("/file/map/mindmap.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")

    with session_scope() as session:
        run = session.query(StageRun).filter_by(file_id="map", stage=StageName.mind_map).one()
        run.detail = {"stale": True}
    assert client.get("/file/map/mindmap.png").status_code == 409
