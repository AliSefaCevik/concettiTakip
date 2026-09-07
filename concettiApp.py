"""Concetti Hat Takibi — telefon kilitlense dahi kayıt kaybetmeyen Streamlit pilotu."""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import streamlit as st


APP_DIR = Path(__file__).resolve().parent
LOCAL_DB = APP_DIR / "concetti_demo.db"
TIMEZONE = ZoneInfo("Europe/Istanbul")

STATIONS = [
    "Tohum kabul ve silo besleme",
    "Boş torba alma ve torba açma",
    "Dolum ve 25 kg tartım",
    "Isıtıcı çene ile torba kapatma",
    "Torba yırtık / dökülme kontrolü",
    "Son ağırlık kontrolü ve red",
    "Etiketleme ve inkjet yazdırma",
    "Palete istifleme",
    "Muşamba ve streç sarma",
    "Teslim alanı ve forklift bekleme",
]

QUICK_EVENTS: dict[str, dict[str, str]] = {
    "Torba yırtığı": {
        "station": "Torba yırtık / dökülme kontrolü",
        "category": "Kalite",
        "note": "Hızlı sayım: Yırtık/dökülme algılandı.",
    },
    "Parazit torba": {
        "station": "Boş torba alma ve torba açma",
        "category": "Uyarı",
        "note": "Hızlı sayım: Ünite aynı anda iki torba aldı; hatta olmaması gereken torba sensör alanında sıkıştı.",
    },
    "Muşamba tutucu tutamadı": {
        "station": "Muşamba ve streç sarma",
        "category": "Uyarı",
        "note": "Hızlı sayım: Muşamba tutucu bobinden muşambayı alamadı.",
    },
    "Palet yapışması": {
        "station": "Palete istifleme",
        "category": "Uyarı",
        "note": "Hızlı sayım: Boş paletler iç içe/yapışık kaldı ve palet düşmedi.",
    },
    "Uygunsuz ağırlık red": {
        "station": "Son ağırlık kontrolü ve red",
        "category": "Kalite",
        "note": "Hızlı sayım: Torba ağırlık kontrolünde reddedildi.",
    },
    "Boş torba alma hatası": {
        "station": "Boş torba alma ve torba açma",
        "category": "Uyarı",
        "note": "Hızlı sayım: Vakumlama, alma veya torba ağzı açma problemi.",
    },
}


class StoreError(RuntimeError):
    """Kullanıcıya gösterilebilecek veri kaydetme/okuma hatası."""


def now_istanbul() -> datetime:
    return datetime.now(TIMEZONE)


def iso_now() -> str:
    return now_istanbul().isoformat()


def current_shift() -> tuple[str, str]:
    """12:00–00:00 ve 00:00–12:00 vardiyalarından aktif olanını verir."""
    current = now_istanbul()
    if current.hour >= 12:
        start = current.replace(hour=12, minute=0, second=0, microsecond=0)
        next_midnight = start.replace(hour=0) + timedelta(days=1)
        label = f"{start:%d %B} 12:00 – {next_midnight:%d %B} 00:00"
    else:
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        label = f"{start:%d %B} 00:00 – {start:%d %B} 12:00"
    return start.isoformat(), label


def format_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        value = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TIMEZONE)
        return parsed.astimezone(TIMEZONE).strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return value


def duration_minutes(start: str, end: str | None = None) -> int:
    try:
        parsed_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        parsed_end = datetime.fromisoformat((end or iso_now()).replace("Z", "+00:00"))
        if parsed_start.tzinfo is None:
            parsed_start = parsed_start.replace(tzinfo=TIMEZONE)
        if parsed_end.tzinfo is None:
            parsed_end = parsed_end.replace(tzinfo=TIMEZONE)
        return max(0, round((parsed_end - parsed_start).total_seconds() / 60))
    except (TypeError, ValueError):
        return 0


def duration_text(minutes: int) -> str:
    return f"{minutes} dk" if minutes < 60 else f"{minutes // 60} sa {minutes % 60} dk"


def app_secret(name: str) -> str | None:
    try:
        value = st.secrets.get(name)
        if value:
            return str(value)
    except FileNotFoundError:
        pass
    return os.environ.get(name)


class SupabaseStore:
    """Supabase REST API üzerinden kalıcı, sunucu taraflı kayıt deposu."""

    persistent = True

    def __init__(self, url: str, service_key: str) -> None:
        self.base_url = url.rstrip("/") + "/rest/v1/"
        self.service_key = service_key

    def _request(
        self,
        method: str,
        table: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        target = self.base_url + table
        if query:
            target += "?" + urllib.parse.urlencode(query)
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            target,
            data=data,
            method=method,
            headers={
                "apikey": self.service_key,
                "Authorization": f"Bearer {self.service_key}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else []
        except urllib.error.HTTPError as error:
            message = error.read().decode("utf-8", errors="replace")
            raise StoreError(f"Sunucu kaydı kabul etmedi ({error.code}): {message}") from error
        except urllib.error.URLError as error:
            raise StoreError("İnternet veya veritabanı bağlantısı kurulamadı. Kayıt gönderilmedi.") from error

    def incidents(self, shift_start: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", "incidents", query={"select": "*", "shift_start": f"eq.{shift_start}", "order": "created_at.desc"}
        )

    def maintenance(self, shift_start: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", "maintenance_logs", query={"select": "*", "shift_start": f"eq.{shift_start}", "order": "start_at.desc"}
        )

    def add_incident(self, row: dict[str, Any]) -> None:
        self._request("POST", "incidents", payload=row)

    def close_incident(self, incident_id: str, note: str) -> None:
        self._request(
            "PATCH",
            "incidents",
            payload={"status": "Kapalı", "ended_at": iso_now(), "close_note": note},
            query={"id": f"eq.{incident_id}"},
        )

    def add_maintenance(self, row: dict[str, Any]) -> None:
        self._request("POST", "maintenance_logs", payload=row)


class SqliteStore:
    """Yalnızca yerel demo için fallback. Community Cloud üzerinde kullanılmamalıdır."""

    persistent = False

    def __init__(self, path: Path) -> None:
        self.path = path
        self._setup()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _setup(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                create table if not exists incidents (
                    id text primary key, created_at text not null, shift_start text not null, shift_name text not null,
                    operator_name text, station text not null, title text not null, category text not null,
                    quantity integer not null, note text, status text not null, ended_at text, close_note text
                );
                create table if not exists maintenance_logs (
                    id text primary key, created_at text not null, shift_start text not null, operator_name text,
                    station text not null, fault_class text not null, title text not null, replaced_part text,
                    start_at text not null, end_at text not null, note text
                );
                """
            )

    def _rows(self, table: str, shift_start: str, order: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            records = connection.execute(
                f"select * from {table} where shift_start = ? order by {order} desc", (shift_start,)
            ).fetchall()
        return [dict(record) for record in records]

    def incidents(self, shift_start: str) -> list[dict[str, Any]]:
        return self._rows("incidents", shift_start, "created_at")

    def maintenance(self, shift_start: str) -> list[dict[str, Any]]:
        return self._rows("maintenance_logs", shift_start, "start_at")

    def add_incident(self, row: dict[str, Any]) -> None:
        row = {"id": str(uuid.uuid4()), "created_at": iso_now(), **row}
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        with self._connection() as connection:
            connection.execute(f"insert into incidents ({columns}) values ({placeholders})", tuple(row.values()))

    def close_incident(self, incident_id: str, note: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "update incidents set status = 'Kapalı', ended_at = ?, close_note = ? where id = ?",
                (iso_now(), note, incident_id),
            )

    def add_maintenance(self, row: dict[str, Any]) -> None:
        row = {"id": str(uuid.uuid4()), "created_at": iso_now(), **row}
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        with self._connection() as connection:
            connection.execute(f"insert into maintenance_logs ({columns}) values ({placeholders})", tuple(row.values()))


def get_store() -> SupabaseStore | SqliteStore:
    url = app_secret("SUPABASE_URL")
    key = app_secret("SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseStore(url, key) if url and key else SqliteStore(LOCAL_DB)


def flash(message: str) -> None:
    st.session_state["flash"] = message


def show_flash() -> None:
    if message := st.session_state.pop("flash", None):
        st.success(message)


def quick_counts(incidents: list[dict[str, Any]]) -> dict[str, int]:
    counts = {title: 0 for title in QUICK_EVENTS}
    for incident in incidents:
        if incident["title"] in counts:
            counts[incident["title"]] += int(incident.get("quantity") or 1)
    return counts


def add_quick_event(store: SupabaseStore | SqliteStore, title: str, shift_start: str, shift_name: str, operator: str) -> None:
    preset = QUICK_EVENTS[title]
    store.add_incident(
        {
            "shift_start": shift_start,
            "shift_name": shift_name,
            "operator_name": operator or None,
            "station": preset["station"],
            "title": title,
            "category": preset["category"],
            "quantity": 1,
            "note": preset["note"],
            "status": "Kapalı",
            "ended_at": iso_now(),
        }
    )
    flash(f"{title}: +1 kaydedildi.")


def style_app() -> None:
    st.markdown(
        """
        <style>
        .block-container {max-width: 1100px; padding-top: 1.5rem; padding-bottom: 3rem;}
        h1 {font-size: 1.75rem !important;}
        div[data-testid="stMetric"] {background: #f5f8f6; border: 1px solid #dce8e0; border-radius: 12px; padding: 11px;}
        div[data-testid="stMetric"] label {color: #64746b;}
        div.stButton > button {min-height: 68px; border-radius: 12px; font-weight: 700; border: 1px solid #b9d7c7;}
        div.stButton > button:hover {border-color: #176c4f; color: #176c4f;}
        @media (max-width: 640px) { .block-container {padding: 1rem .8rem 2rem;} h1 {font-size: 1.45rem !important;} }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_home(store: SupabaseStore | SqliteStore, incidents: list[dict[str, Any]], shift_start: str, shift_name: str, operator: str) -> None:
    st.subheader("Sık yaşanan sorunlar")
    st.caption("Bir tuşa basıldığı anda kayıt sunucuya yazılır. Telefon kilitlense bile kaybolmaz.")
    counts = quick_counts(incidents)
    event_titles = list(QUICK_EVENTS)
    for start in range(0, len(event_titles), 2):
        columns = st.columns(2)
        for column, title in zip(columns, event_titles[start : start + 2]):
            with column:
                st.metric(title, f"{counts[title]} bugün")
                if st.button(f"{title} +1", key=f"quick-{title}", use_container_width=True):
                    try:
                        add_quick_event(store, title, shift_start, shift_name, operator)
                        st.rerun()
                    except StoreError as error:
                        st.error(str(error))
    active = [item for item in incidents if item["status"] == "Açık"]
    st.divider()
    col1, col2, col3 = st.columns(3)
    col1.metric("Açık arıza", len(active))
    col2.metric("Bu vardiya kayıt", len(incidents))
    closed_stops = [item for item in incidents if item["category"] == "Duruş" and item["status"] == "Kapalı"]
    total_stop = sum(duration_minutes(item["created_at"], item.get("ended_at")) for item in closed_stops)
    col3.metric("Kapanmış duruş", duration_text(total_stop))
    if active:
        st.warning(f"{len(active)} açık arıza var. 'Aktif arızalar' sayfasından devralın veya kapatın.")


def render_incident_form(store: SupabaseStore | SqliteStore, shift_start: str, shift_name: str, operator: str) -> None:
    st.subheader("Detaylı hata veya duruş kaydı")
    st.caption("Uzun süren ya da tekrarlayan önemli olaylar için kullanılır.")
    with st.form("incident-form", clear_on_submit=True):
        station = st.selectbox("İstasyon", STATIONS)
        category = st.selectbox("Olay türü", ["Duruş", "Kalite", "Uyarı", "Lojistik", "Yarım palet"])
        title = st.text_input("Hata başlığı", placeholder="Örn. Isıtıcıdan açık torba çıktı")
        quantity = st.number_input("Etkilenen torba / palet", min_value=0, value=0, step=1)
        note = st.text_area("Not / ilk gözlem", placeholder="Ne oldu? Hata kodu veya yapılan ilk müdahale?")
        submit = st.form_submit_button("Kaydı aç", use_container_width=True)
    if submit:
        if not title.strip():
            st.error("Hata başlığını yazın.")
            return
        try:
            store.add_incident(
                {
                    "shift_start": shift_start,
                    "shift_name": shift_name,
                    "operator_name": operator or None,
                    "station": station,
                    "title": title.strip(),
                    "category": category,
                    "quantity": int(quantity),
                    "note": note.strip() or None,
                    "status": "Açık" if category in {"Duruş", "Lojistik"} else "Kapalı",
                    "ended_at": None if category in {"Duruş", "Lojistik"} else iso_now(),
                }
            )
            flash("Kayıt oluşturuldu.")
            st.rerun()
        except StoreError as error:
            st.error(str(error))


def render_active(store: SupabaseStore | SqliteStore, incidents: list[dict[str, Any]]) -> None:
    st.subheader("Aktif arızalar")
    active = [item for item in incidents if item["status"] == "Açık"]
    if not active:
        st.info("Bu vardiyada açık arıza yok.")
        return
    for item in active:
        with st.container(border=True):
            st.markdown(f"**{item['title']}**  ")
            st.caption(f"{item['station']} · {format_time(item['created_at'])} · {duration_text(duration_minutes(item['created_at']))}")
            with st.form(f"close-{item['id']}"):
                note = st.text_input("Müdahale / çözüm notu", key=f"note-{item['id']}")
                close = st.form_submit_button("Arızayı çözüldü olarak kapat", use_container_width=True)
            if close:
                try:
                    store.close_incident(item["id"], note.strip())
                    flash("Arıza kapatıldı; süre rapora işlendi.")
                    st.rerun()
                except StoreError as error:
                    st.error(str(error))


def render_maintenance(store: SupabaseStore | SqliteStore, maintenance: list[dict[str, Any]], shift_start: str, operator: str) -> None:
    st.subheader("Arıza kayıt defteri")
    st.caption("Zincir, alyan vida, rulman gibi büyük arızaların bakım geçmişi.")
    with st.form("maintenance-form", clear_on_submit=True):
        station = st.selectbox("İstasyon", STATIONS, key="maintenance-station")
        fault_class = st.selectbox("Arıza sınıfı", ["Mekanik", "Elektrik", "Sensör", "Otomasyon", "Diğer"])
        title = st.text_input("Arıza başlığı", placeholder="Örn. Dolum zinciri koptu")
        replaced_part = st.text_input("Değişen parça", placeholder="Örn. M8 alyan vida")
        start_at = st.datetime_input("Başlangıç", value=now_istanbul())
        end_at = st.datetime_input("Çözüm", value=now_istanbul())
        note = st.text_area("Neden, işlem ve önlem")
        submit = st.form_submit_button("Bakım kaydını ekle", use_container_width=True)
    if submit:
        if not title.strip():
            st.error("Arıza başlığını yazın.")
        elif end_at < start_at:
            st.error("Çözüm zamanı başlangıçtan önce olamaz.")
        else:
            try:
                store.add_maintenance(
                    {
                        "shift_start": shift_start,
                        "operator_name": operator or None,
                        "station": station,
                        "fault_class": fault_class,
                        "title": title.strip(),
                        "replaced_part": replaced_part.strip() or None,
                        "start_at": start_at.replace(tzinfo=TIMEZONE).isoformat(),
                        "end_at": end_at.replace(tzinfo=TIMEZONE).isoformat(),
                        "note": note.strip() or None,
                    }
                )
                flash("Bakım kaydı deftere eklendi.")
                st.rerun()
            except StoreError as error:
                st.error(str(error))
    st.divider()
    if maintenance:
        st.dataframe(
            [
                {
                    "Başlangıç": format_time(item["start_at"]),
                    "İstasyon": item["station"],
                    "Arıza": item["title"],
                    "Parça": item.get("replaced_part") or "—",
                    "Süre": duration_text(duration_minutes(item["start_at"], item["end_at"])),
                }
                for item in maintenance
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("Bu vardiyada bakım defteri kaydı yok.")


def render_report(incidents: list[dict[str, Any]], maintenance: list[dict[str, Any]], shift_name: str) -> None:
    st.subheader("Vardiya raporu")
    st.caption(shift_name)
    counts = quick_counts(incidents)
    stops = [item for item in incidents if item["category"] == "Duruş"]
    total_stop = sum(duration_minutes(item["created_at"], item.get("ended_at")) for item in stops if item["status"] == "Kapalı")
    open_stops = sum(1 for item in stops if item["status"] == "Açık")
    metrics = st.columns(4)
    metrics[0].metric("Toplam olay", len(incidents))
    metrics[1].metric("Toplam duruş", duration_text(total_stop))
    metrics[2].metric("Açık duruş", open_stops)
    metrics[3].metric("Bakım kaydı", len(maintenance))
    st.markdown("#### Sık sorunlar")
    st.dataframe(
        [{"Olay": name, "Adet": count} for name, count in counts.items()],
        use_container_width=True,
        hide_index=True,
    )
    if incidents:
        station_counts: dict[str, int] = {}
        for item in incidents:
            station_counts[item["station"]] = station_counts.get(item["station"], 0) + 1
        top_station, top_count = max(station_counts.items(), key=lambda entry: entry[1])
        st.info(f"En çok kayıt görülen istasyon: **{top_station}** ({top_count} kayıt).")
        rows = [
            {
                "Saat": format_time(item["created_at"]),
                "İstasyon": item["station"],
                "Olay": item["title"],
                "Tür": item["category"],
                "Durum": item["status"],
                "Adet": item.get("quantity") or 0,
            }
            for item in incidents
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)
        csv = "Tarih;İstasyon;Olay;Tür;Durum;Adet\n" + "\n".join(
            f'"{row["Saat"]}";"{row["İstasyon"]}";"{row["Olay"]}";"{row["Tür"]}";"{row["Durum"]}";{row["Adet"]}'
            for row in rows
        )
        st.download_button("Vardiya kayıtlarını CSV indir", csv.encode("utf-8-sig"), "concetti-vardiya.csv", "text/csv")
    else:
        st.info("Bu vardiyada henüz kayıt yok.")


def main() -> None:
    st.set_page_config(page_title="Concetti Hat Takibi", page_icon="C", layout="wide")
    style_app()
    store = get_store()
    shift_start, shift_name = current_shift()

    with st.sidebar:
        st.title("Concetti")
        st.caption("Hat takip pilotu")
        operator = st.text_input("Vardiya sorumlusu", placeholder="İsteğe bağlı")
        page = st.radio("Menü", ["Hızlı sayım", "Detaylı kayıt", "Aktif arızalar", "Arıza kayıt defteri", "Rapor"], label_visibility="collapsed")
        st.divider()
        st.caption(shift_name)
        if store.persistent:
            st.success("Merkezî veritabanı bağlı")
        else:
            st.warning("Yerel demo modu: yayınlamadan önce Supabase ayarlarını ekleyin.")

    st.title("Concetti Hat Takibi")
    show_flash()
    try:
        incidents = store.incidents(shift_start)
        maintenance = store.maintenance(shift_start)
    except StoreError as error:
        st.error(str(error))
        st.stop()

    if page == "Hızlı sayım":
        render_home(store, incidents, shift_start, shift_name, operator)
    elif page == "Detaylı kayıt":
        render_incident_form(store, shift_start, shift_name, operator)
    elif page == "Aktif arızalar":
        render_active(store, incidents)
    elif page == "Arıza kayıt defteri":
        render_maintenance(store, maintenance, shift_start, operator)
    else:
        render_report(incidents, maintenance, shift_name)


if __name__ == "__main__":
    main()
