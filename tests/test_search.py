from memory_core.types import Scope


def test_ranked_search_keeps_scope_time_role_and_pagination(lab):
    service,scope=lab
    rows=[]
    for i in range(5):
        row=service.record_message(scope,"南门红伞 "+"补充 "*i,f"search_{i}")
        service.db.execute("UPDATE ltm_messages SET created=? WHERE id=?",(100+i,row["id"]))
        rows.append(row)
    reply=service.record_message(scope,"南门红伞","reply",role="assistant")
    service.db.execute("UPDATE ltm_messages SET created=102 WHERE id=?",(reply["id"],))
    private_scope=Scope(service.persona_id,"other",service.new_session(Scope(service.persona_id,"other")))
    service.record_message(private_scope,"南门红伞","private")
    filters={"since":101,"until":103,"role":"user","session_id":scope.session_id}
    for relaxed in (False,True):
        for newest in (False,True):
            hits=service.search_messages(scope,"红伞",filters,limit=10,relaxed=relaxed,newest=newest)
            assert {r["id"] for r in hits}=={r["id"] for r in rows[1:4]}
            pages=[service.search_messages(scope,"红伞",filters,limit=1,cursor=i,relaxed=relaxed,newest=newest)[0]["id"] for i in range(3)]
            assert pages==[r["id"] for r in hits]
    history=service.search_messages(scope,"",filters,limit=10)
    assert [r["created"] for r in history]==[103,102,101]
