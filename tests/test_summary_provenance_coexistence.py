def test_cloud_and_local_same_template_can_coexist(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings
    from localplaud.db.models import PlaudFile, Summary
    from localplaud.db.session import init_db, session_scope

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'summary.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    init_db()
    with session_scope() as session:
        session.add(PlaudFile(id="r1"))
        session.add(Summary(file_id="r1", template="default", source="cloud",
                            content_md="imported"))
        session.add(Summary(file_id="r1", template="default", source="local",
                            content_md="independent"))
    with session_scope() as session:
        rows = session.query(Summary).filter_by(file_id="r1", template="default").all()
        assert {row.source for row in rows} == {"cloud", "local"}
