# Smoke test FLOW 4: portál "Přidat další hotel" -> Stripe subscription item -> novy hotel
import asyncio, importlib.util, json, os, sys
import unittest.mock as mock

sys.path.insert(0, "/tmp")
spec = importlib.util.spec_from_file_location("appmod", "/tmp/app.py")
appmod = importlib.util.module_from_spec(spec)
with mock.patch.dict(os.environ, {"ADMIN_PASSWORD": "x"}):
    spec.loader.exec_module(appmod)

SETTINGS = {"stripe_secret_key": "sk_test", "pricing_base": 199,
            "pricing_threshold": 100, "pricing_per_bed": 2}
PARENT = {"id": "p1", "hotel_token": "tokP", "name": "Hotel Parent",
          "subscription_active": True, "stripe_subscription_id": "sub_123",
          "stripe_customer_id": "cus_1", "subscription_period_end": "2026-09-01",
          "email": "own@x.com", "registration_email": "own@x.com",
          "phone": "+420", "contact_name": "Owner", "country": "CZ",
          "ico": "123", "dic": "", "billing_name": "Firma", "trial_used": True}
CHILD = {"id": "c1", "hotel_token": "tokC", "name": "Child", "group_parent": "p1",
         "subscription_active": True, "email": "own@x.com"}
NOPAY = {"id": "n1", "hotel_token": "tokN", "name": "NoPay", "subscription_active": False}
DB = {"hotels": {"p1": dict(PARENT), "c1": dict(CHILD), "n1": dict(NOPAY)}, "settings": {}}

class FakeReq:
    headers = {"host": "x"}
    url = type("U", (), {"scheme": "https"})()

STRIPE = []
class FakeResp:
    def __init__(self, status, data): self.status_code=status; self._d=data; self.text=json.dumps(data)
    def json(self): return self._d
class FakeClient:
    def __init__(self,*a,**k): pass
    async def __aenter__(self): return self
    async def __aexit__(self,*a): return False
    async def post(self,url,**kw):
        STRIPE.append((url,kw.get("data") or {}))
        if url.endswith("/products"): return FakeResp(200, {"id":"prod_9"})
        if url.endswith("/subscription_items"): return FakeResp(200, {"id":"si_7"})
        return FakeResp(404, {})

spawned=[]
def patches():
    return [mock.patch.object(appmod,"db_load",lambda: DB),
            mock.patch.object(appmod,"db_save",lambda d: None),
            mock.patch.object(appmod,"db_get_settings",lambda: SETTINGS),
            mock.patch.object(appmod,"get_base_url",lambda r: "https://x"),
            mock.patch.object(appmod,"_spawn",lambda c:(spawned.append(getattr(c,'__name__',str(c))), c.close())),
            mock.patch.object(appmod,"find_hotel_by_token",
                lambda t: {"tokP":DB["hotels"]["p1"],"tokC":DB["hotels"]["c1"],"tokN":DB["hotels"]["n1"]}.get(t)),
            mock.patch.object(appmod.httpx,"AsyncClient",FakeClient)]

async def main():
    ps=patches()
    for p in ps: p.__enter__()
    try:
        R=appmod.PortalAddHotelRequest
        # 1) parent pridava hotel
        r=await appmod.portal_add_hotel(R(token="tokP",name="Hotel Nova",beds=120,url="www.nova.cz"),FakeReq())
        assert r["status"]=="ok" and r["monthly_price_eur"]==199+20*2, r
        nid=r["hotel_id"]; nh=DB["hotels"][nid]
        assert nh["group_parent"]=="p1" and nh["origin"]=="standard_group"
        assert nh["stripe_subscription_item_id"]=="si_7"
        assert nh["email"]=="own@x.com" and nh["billing_name"]=="Firma"
        assert nh["bed_count"]==120 and nh["subscription_price"]==239
        assert nh["url"]=="https://www.nova.cz" and nh["trial_used"] is True
        assert nh["subscription_active"] is True
        # Stripe: product + item na sub_123 s prorations
        assert STRIPE[0][0].endswith("/products")
        assert STRIPE[1][0].endswith("/subscription_items")
        assert STRIPE[1][1]["subscription"]=="sub_123"
        assert STRIPE[1][1]["price_data[unit_amount]"]==str(239*100)
        assert STRIPE[1][1]["proration_behavior"]=="create_prorations"
        # e-mail + scraper spawnnuty
        assert len(spawned)==2, spawned
        # 2) child portal -> billing jde pres parenta
        STRIPE.clear()
        r2=await appmod.portal_add_hotel(R(token="tokC",name="Hotel Dva",beds=10),FakeReq())
        assert DB["hotels"][r2["hotel_id"]]["group_parent"]=="p1"
        assert STRIPE[1][1]["subscription"]=="sub_123"
        assert r2["monthly_price_eur"]==199
        # 3) bez predplatneho -> 400
        try:
            await appmod.portal_add_hotel(R(token="tokN",name="Hotel X",beds=10),FakeReq())
            assert False, "melo spadnout"
        except appmod.HTTPException as e:
            assert e.status_code==400
        # 4) validace
        for bad in [R(token="tokP",name="A",beds=10), R(token="tokP",name="Hotel",beds=0)]:
            try:
                await appmod.portal_add_hotel(bad,FakeReq()); assert False
            except appmod.HTTPException as e:
                assert e.status_code==400
        print("FLOW4 TEST OK - vsech 6 scenaru proslo")
    finally:
        for p in ps: p.stop()

asyncio.run(main())
