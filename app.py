import csv
import io
import json
import math
import tempfile
import uuid
import hmac
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import psycopg
import streamlit as st
import streamlit.components.v1 as components

CATEGORIES = ["Pre-A1", "A1", "A2", "B1", "B2"]
UNASSIGNED = "Noch nicht zugeordnet"

LOREM_SENTENCES = [
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
    "Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.",
    "Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris.",
    "Duis aute irure dolor in reprehenderit in voluptate velit esse cillum.",
    "Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia.",
]

CARDS = {
    f"K{i:02d}": LOREM_SENTENCES[(i - 1) % len(LOREM_SENTENCES)]
    for i in range(1, 51)
}

LABEL_TO_ID = {
    f"{card_id} · {text}": card_id
    for card_id, text in CARDS.items()
}

ID_TO_LABEL = {
    card_id: label
    for label, card_id in LABEL_TO_ID.items()
}


# -------------------------------------------------------------------
# Board / Daten
# -------------------------------------------------------------------

def initial_board():
    return [
        {
            "header": UNASSIGNED,
            "items": [
                ID_TO_LABEL[card_id]
                for card_id in CARDS
            ],
        },
        *[
            {
                "header": category,
                "items": [],
            }
            for category in CATEGORIES
        ],
    ]


def board_to_assignments(board):
    assignments = {}

    for container in board:
        category = container["header"]

        if category == UNASSIGNED:
            continue

        for label in container["items"]:
            card_id = LABEL_TO_ID.get(label)

            if card_id:
                assignments[card_id] = category

    return assignments


def validate_board(board):
    expected_headers = [UNASSIGNED, *CATEGORIES]

    if not isinstance(board, list):
        return {
            "unassigned_count": len(CARDS),
            "duplicate_count": 0,
            "missing_count": len(CARDS),
            "unknown_count": 0,
            "is_valid": False,
        }

    if len(board) != len(expected_headers):
        return {
            "unassigned_count": len(CARDS),
            "duplicate_count": 0,
            "missing_count": len(CARDS),
            "unknown_count": 0,
            "is_valid": False,
        }

    headers = []
    all_items = []
    unassigned_count = 0

    try:
        for container in board:
            header = container["header"]
            items = list(container["items"])

            headers.append(header)
            all_items.extend(items)

            if header == UNASSIGNED:
                unassigned_count = len(items)

    except (KeyError, TypeError):
        return {
            "unassigned_count": len(CARDS),
            "duplicate_count": 0,
            "missing_count": len(CARDS),
            "unknown_count": 0,
            "is_valid": False,
        }

    expected_items = set(LABEL_TO_ID)
    actual_items = set(all_items)

    duplicate_count = len(all_items) - len(actual_items)
    missing = expected_items - actual_items
    unknown = actual_items - expected_items

    structure_ok = (
        headers == expected_headers
        and len(all_items) == len(CARDS)
        and duplicate_count == 0
        and not missing
        and not unknown
    )

    return {
        "unassigned_count": unassigned_count,
        "duplicate_count": duplicate_count,
        "missing_count": len(missing),
        "unknown_count": len(unknown),
        "is_valid": (
            structure_ok
            and unassigned_count == 0
        ),
    }


def is_safe_board_payload(board):
    """
    Prüft, ob der Browser einen plausiblen Board-Zustand
    zurückgegeben hat, bevor er in den Session State übernommen wird.
    """
    if not isinstance(board, list):
        return False

    if len(board) != 6:
        return False

    expected_headers = [UNASSIGNED, *CATEGORIES]

    try:
        headers = [
            container["header"]
            for container in board
        ]

        all_items = [
            item
            for container in board
            for item in container["items"]
        ]

    except (KeyError, TypeError):
        return False

    return (
        headers == expected_headers
        and len(all_items) == len(CARDS)
        and len(set(all_items)) == len(CARDS)
        and set(all_items) == set(LABEL_TO_ID)
    )


# -------------------------------------------------------------------
# Datenbank
# -------------------------------------------------------------------

# -------------------------------------------------------------------
# Datenbank: Supabase / PostgreSQL
# -------------------------------------------------------------------

def get_connection():
    """
    Öffnet eine verschlüsselte Verbindung zur
    PostgreSQL-Datenbank bei Supabase.

    DATABASE_URL liegt als Secret in
    Streamlit Community Cloud.
    """
    return psycopg.connect(
        str(st.secrets["DATABASE_URL"]),
        sslmode="require",
        connect_timeout=10,
    )


@st.cache_resource
def init_db():
    """
    Prüft/erstellt die benötigten Tabellen einmal
    pro Start der Streamlit-App.

    Durch @st.cache_resource wird dieser Code nicht
    bei jedem Verschieben einer Karte erneut ausgeführt.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS public.submissions (
                    submission_id UUID PRIMARY KEY,
                    participant_id TEXT NOT NULL UNIQUE,
                    submitted_at_utc TIMESTAMPTZ NOT NULL
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS public.assignments (
                    submission_id UUID NOT NULL
                        REFERENCES public.submissions(submission_id)
                        ON DELETE CASCADE,

                    card_id TEXT NOT NULL,

                    category TEXT NOT NULL
                        CHECK (
                            category IN (
                                'Pre-A1',
                                'A1',
                                'A2',
                                'B1',
                                'B2'
                            )
                        ),

                    PRIMARY KEY (
                        submission_id,
                        card_id
                    )
                )
                """
            )

            # Zusätzliche Absicherung:
            # RLS bleibt für beide Tabellen aktiviert.
            cur.execute(
                """
                ALTER TABLE public.submissions
                ENABLE ROW LEVEL SECURITY
                """
            )

            cur.execute(
                """
                ALTER TABLE public.assignments
                ENABLE ROW LEVEL SECURITY
                """
            )

    return True


def save_submission(
    participant_id,
    assignments,
):
    """
    Speichert eine vollständige Abgabe als eine
    PostgreSQL-Transaktion.

    Entweder werden die Abgabe UND alle 50 Zuordnungen
    gespeichert oder gar nichts.
    """

    submission_id = uuid.uuid4()

    submitted_at = datetime.now(
        timezone.utc
    )

    rows = [
        (
            submission_id,
            card_id,
            category,
        )
        for card_id, category
        in sorted(assignments.items())
    ]

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO public.submissions (
                    submission_id,
                    participant_id,
                    submitted_at_utc
                )
                VALUES (%s, %s, %s)
                """,
                (
                    submission_id,
                    participant_id,
                    submitted_at,
                ),
            )

            cur.executemany(
                """
                INSERT INTO public.assignments (
                    submission_id,
                    card_id,
                    category
                )
                VALUES (%s, %s, %s)
                """,
                rows,
            )

    return str(submission_id)
def load_results():
    """
    Lädt alle bisher abgegebenen Ergebnisse aus Supabase/PostgreSQL.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    s.participant_id,
                    s.submitted_at_utc,
                    a.card_id,
                    a.category
                FROM public.submissions AS s
                JOIN public.assignments AS a
                    ON a.submission_id = s.submission_id
                ORDER BY
                    s.submitted_at_utc,
                    s.participant_id,
                    a.card_id
                """
            )

            return cur.fetchall()


def create_results_csv(rows):
    """
    Erzeugt die Rohdaten-CSV vollständig im Arbeitsspeicher.
    """
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(
        [
            "participant_id",
            "submitted_at_utc",
            "card_id",
            "category",
        ]
    )

    for (
        participant_id,
        submitted_at,
        card_id,
        category,
    ) in rows:
        if hasattr(
            submitted_at,
            "isoformat",
        ):
            submitted_at = submitted_at.isoformat()

        writer.writerow(
            [
                participant_id,
                submitted_at,
                card_id,
                category,
            ]
        )

    return output.getvalue().encode(
        "utf-8-sig"
    )


def results_to_dataframe(rows):
    """
    Wandelt die Rohdaten aus PostgreSQL in einen DataFrame um.
    """
    df = pd.DataFrame(
        rows,
        columns=[
            "participant_id",
            "submitted_at_utc",
            "card_id",
            "category",
        ],
    )

    if not df.empty:
        df["submitted_at_utc"] = pd.to_datetime(
            df["submitted_at_utc"],
            utc=True,
        )

    return df


def build_submission_overview(raw_df):
    """
    Eine Zeile pro Submission/Teilnehmer-ID.
    """
    if raw_df.empty:
        return pd.DataFrame(
            columns=[
                "Teilnehmer-ID",
                "Zeitpunkt (UTC)",
                "Karten",
            ]
        )

    grouped = (
        raw_df
        .groupby(
            [
                "participant_id",
                "submitted_at_utc",
            ],
            as_index=False,
        )
        .agg(
            Karten=(
                "card_id",
                "nunique",
            )
        )
        .sort_values(
            "submitted_at_utc",
            ascending=False,
        )
    )

    grouped["Zeitpunkt (UTC)"] = (
        grouped["submitted_at_utc"]
        .dt.strftime(
            "%d.%m.%Y %H:%M:%S UTC"
        )
    )

    grouped = grouped.rename(
        columns={
            "participant_id":
                "Teilnehmer-ID",
        }
    )

    return grouped[
        [
            "Teilnehmer-ID",
            "Zeitpunkt (UTC)",
            "Karten",
        ]
    ]


def build_card_analysis(raw_df):
    """
    Berechnet pro Karte:
    - Häufigkeit je CEFR-Kategorie
    - Mehrheitskategorie
    - Übereinstimmung
    - Dissens
    - Spannweite
    - normalisierte Entropie
    """
    rows = []

    category_positions = {
        category: index
        for index, category
        in enumerate(CATEGORIES)
    }

    for card_id in sorted(CARDS):
        card_rows = raw_df[
            raw_df["card_id"]
            == card_id
        ]

        counts_series = (
            card_rows["category"]
            .value_counts()
            .reindex(
                CATEGORIES,
                fill_value=0,
            )
        )

        counts = {
            category:
                int(counts_series[category])
            for category
            in CATEGORIES
        }

        number_ratings = int(
            counts_series.sum()
        )

        if number_ratings > 0:
            max_count = int(
                counts_series.max()
            )

            modal_categories = [
                category
                for category
                in CATEGORIES
                if (
                    counts[category]
                    == max_count
                )
            ]

            majority = " / ".join(
                modal_categories
            )

            agreement = (
                max_count
                / number_ratings
                * 100
            )

            dissent = (
                100
                - agreement
            )

            used_positions = [
                category_positions[
                    category
                ]
                for category
                in CATEGORIES
                if counts[category] > 0
            ]

            spread = (
                max(used_positions)
                - min(used_positions)
            )

            probabilities = [
                counts[category]
                / number_ratings
                for category
                in CATEGORIES
                if counts[category] > 0
            ]

            entropy = (
                -sum(
                    probability
                    * math.log(
                        probability
                    )
                    for probability
                    in probabilities
                )
                / math.log(
                    len(CATEGORIES)
                )
                * 100
            )

        else:
            majority = "—"
            agreement = 0.0
            dissent = 0.0
            spread = 0
            entropy = 0.0

        rows.append(
            {
                "Karte":
                    card_id,

                "Text":
                    CARDS[card_id],

                "Bewertungen":
                    number_ratings,

                "Pre-A1":
                    counts["Pre-A1"],

                "A1":
                    counts["A1"],

                "A2":
                    counts["A2"],

                "B1":
                    counts["B1"],

                "B2":
                    counts["B2"],

                "Mehrheit":
                    majority,

                "Übereinstimmung %":
                    round(
                        agreement,
                        1,
                    ),

                "Dissens %":
                    round(
                        dissent,
                        1,
                    ),

                "Spannweite":
                    spread,

                "Entropie %":
                    round(
                        entropy,
                        1,
                    ),
            }
        )

    return pd.DataFrame(
        rows
    )



def full_table_height(df):
    """
    Berechnet genug Höhe, damit ein st.dataframe
    alle Zeilen ohne eigenen vertikalen Scrollbereich zeigt.
    """
    row_height = 35
    header_height = 40
    padding = 8

    return (
        header_height
        + len(df) * row_height
        + padding
    )
def render_admin_dashboard(
    rows,
):
    """
    Das eigentliche Dashboard mit fünf Ansichten.
    """
    raw_df = results_to_dataframe(
        rows
    )

    submissions_df = (
        build_submission_overview(
            raw_df
        )
    )

    card_analysis_df = (
        build_card_analysis(
            raw_df
        )
    )

    number_submissions = len(
        submissions_df
    )

    number_assignments = len(
        raw_df
    )

    perfect_consensus_count = int(
        (
            card_analysis_df[
                "Übereinstimmung %"
            ]
            == 100.0
        ).sum()
    )

    low_agreement_count = int(
        (
            (
                card_analysis_df[
                    "Übereinstimmung %"
                ]
                < 50.0
            )
            & (
                card_analysis_df[
                    "Bewertungen"
                ]
                > 0
            )
        ).sum()
    )

    if submissions_df.empty:
        last_submission = "—"
    else:
        last_submission = (
            submissions_df.iloc[0][
                "Zeitpunkt (UTC)"
            ]
        )

    st.title(
        "📊 Admin-Dashboard"
    )

    top_left, top_right = (
        st.columns(
            [3, 1]
        )
    )

    with top_left:
        st.caption(
            "Die Kennzahlen werden live aus "
            "Supabase/PostgreSQL berechnet."
        )

    with top_right:
        if st.button(
            "← Zur Umfrage",
            use_container_width=True,
        ):
            st.session_state[
                "admin_dashboard_open"
            ] = False

            st.rerun()

    (
        tab_overview,
        tab_submissions,
        tab_cards,
        tab_consensus,
        tab_dissent,
    ) = st.tabs(
        [
            "📌 Überblick",
            "🧾 Abgaben",
            "🗂️ Kartenanalyse",
            "✅ TOP Konsens",
            "⚠️ Problemfälle",
        ]
    )

    # ---------------------------------------------------------
    # 1. Überblick
    # ---------------------------------------------------------
    with tab_overview:
        (
            metric_1,
            metric_2,
            metric_3,
            metric_4,
        ) = st.columns(4)

        metric_1.metric(
            "Abgaben",
            number_submissions,
        )

        metric_2.metric(
            "Zuordnungen",
            number_assignments,
        )

        metric_3.metric(
            "100 % Konsens",
            perfect_consensus_count,
        )

        metric_4.metric(
            "< 50 % Übereinstimmung",
            low_agreement_count,
        )

        st.write(
            f"**Letzte Abgabe:** "
            f"{last_submission}"
        )

        st.divider()

        st.subheader(
            "Downloads"
        )

        raw_csv = (
            create_results_csv(
                rows
            )
        )

        analysis_csv = (
            card_analysis_df
            .to_csv(
                index=False,
            )
            .encode(
                "utf-8-sig"
            )
        )

        download_1, download_2 = (
            st.columns(2)
        )

        with download_1:
            st.download_button(
                label=(
                    "📥 Rohdaten als CSV"
                ),
                data=raw_csv,
                file_name=(
                    "ergebnisse_rohdaten.csv"
                ),
                mime="text/csv",
                use_container_width=True,
            )

        with download_2:
            st.download_button(
                label=(
                    "📥 Kartenanalyse als CSV"
                ),
                data=analysis_csv,
                file_name=(
                    "kartenanalyse.csv"
                ),
                mime="text/csv",
                use_container_width=True,
            )

        st.caption(
            "Übereinstimmung = Anteil der häufigsten "
            "Einstufung. Spannweite = Abstand zwischen "
            "der niedrigsten und höchsten verwendeten "
            "CEFR-Kategorie. Entropie beschreibt, wie "
            "stark sich die Antworten über mehrere "
            "Kategorien verteilen."
        )

    # ---------------------------------------------------------
    # 2. Alle Submissions
    # ---------------------------------------------------------
    with tab_submissions:
        st.subheader(
            "Alle Abgaben"
        )

        if submissions_df.empty:
            st.info(
                "Noch keine Abgaben vorhanden."
            )

        else:
            st.dataframe(
                submissions_df,
                hide_index=True,
                use_container_width=True,
                height=full_table_height(
                    submissions_df
                ),
            )

    # ---------------------------------------------------------
    # 3. Analyse aller Karten
    # ---------------------------------------------------------
    with tab_cards:
        st.subheader(
            "Alle Karteikarten"
        )

        st.dataframe(
            card_analysis_df,
            hide_index=True,
            use_container_width=True,
            height=full_table_height(
                card_analysis_df
            ),
            column_config={
                "Übereinstimmung %":
                    st.column_config.ProgressColumn(
                        "Übereinstimmung %",
                        min_value=0,
                        max_value=100,
                        format="%.1f %%",
                    ),
        
                "Dissens %":
                    st.column_config.NumberColumn(
                        "Dissens %",
                        format="%.1f %%",
                    ),
        
                "Entropie %":
                    st.column_config.NumberColumn(
                        "Entropie %",
                        format="%.1f %%",
                    ),
            },
        )

    # ---------------------------------------------------------
    # 4. TOP 10 mit vollständigem Konsens
    # ---------------------------------------------------------
    with tab_consensus:
        st.subheader(
            "TOP 10 – vollständiger Konsens"
        )

        st.write(
            "Hier erscheinen nur Karten, bei denen "
            "**100 % der Teilnehmenden dieselbe "
            "Kategorie gewählt haben**."
        )

        perfect_df = (
            card_analysis_df[
                (
                    card_analysis_df[
                        "Übereinstimmung %"
                    ]
                    == 100.0
                )
                & (
                    card_analysis_df[
                        "Bewertungen"
                    ]
                    > 0
                )
            ]
            .sort_values(
                [
                    "Bewertungen",
                    "Karte",
                ],
                ascending=[
                    False,
                    True,
                ],
            )
            .head(10)
        )

        if perfect_df.empty:
            st.info(
                "Aktuell gibt es keine Karte "
                "mit 100 % Übereinstimmung."
            )

        else:
            st.dataframe(
                perfect_df[
                    [
                        "Karte",
                        "Text",
                        "Bewertungen",
                        "Mehrheit",
                        "Übereinstimmung %",
                    ]
                ],
                hide_index=True,
                use_container_width=True,
            )

            if (
                perfect_consensus_count
                > 10
            ):
                st.caption(
                    f"Insgesamt gibt es "
                    f"{perfect_consensus_count} "
                    "Karten mit 100 % Konsens. "
                    "Angezeigt werden die ersten 10."
                )

    # ---------------------------------------------------------
    # 5. Problemfälle / Dissens
    # ---------------------------------------------------------
    with tab_dissent:
        st.subheader(
            "Besonders problematische Karten"
        )

        st.write(
            "Sortierung: zuerst **geringe "
            "Übereinstimmung**, bei Gleichstand "
            "eine **größere Spannweite** und danach "
            "eine **höhere Entropie**."
        )

        problematic_df = (
            card_analysis_df[
                card_analysis_df[
                    "Bewertungen"
                ]
                > 0
            ]
            .sort_values(
                [
                    "Übereinstimmung %",
                    "Spannweite",
                    "Entropie %",
                ],
                ascending=[
                    True,
                    False,
                    False,
                ],
            )
            .head(10)
        )

        if problematic_df.empty:
            st.info(
                "Noch keine bewerteten Karten vorhanden."
            )

        else:
            st.dataframe(
                problematic_df[
                    [
                        "Karte",
                        "Text",
                        "Bewertungen",
                        "Mehrheit",
                        "Übereinstimmung %",
                        "Dissens %",
                        "Spannweite",
                        "Entropie %",
                    ]
                ],
                hide_index=True,
                use_container_width=True,
                height=full_table_height(
                    problematic_df
                ),
                column_config={
                    "Übereinstimmung %":
                        st.column_config.ProgressColumn(
                            "Übereinstimmung %",
                            min_value=0,
                            max_value=100,
                            format="%.1f %%",
                        ),
            
                    "Dissens %":
                        st.column_config.NumberColumn(
                            "Dissens %",
                            format="%.1f %%",
                        ),
            
                    "Entropie %":
                        st.column_config.NumberColumn(
                            "Entropie %",
                            format="%.1f %%",
                        ),
                },
            )

def render_admin_area():
    """
    Login in der Seitenleiste.
    Nach erfolgreicher Anmeldung wird das Dashboard
    im Hauptbereich geöffnet.
    """
    with st.sidebar:
        with st.expander(
            "🔒 Admin-Bereich",
            expanded=False,
        ):

            if not st.session_state.get(
                "admin_granted",
                False,
            ):
                with st.form(
                    "admin_login_form"
                ):
                    entered_admin_code = (
                        st.text_input(
                            "Admin-Code",
                            type="password",
                        )
                    )

                    login = (
                        st.form_submit_button(
                            "Admin öffnen"
                        )
                    )

                if login:
                    try:
                        correct_admin_code = str(
                            st.secrets[
                                "ADMIN_CODE"
                            ]
                        )

                    except Exception:
                        st.error(
                            "ADMIN_CODE wurde noch "
                            "nicht in den "
                            "Streamlit-Secrets "
                            "konfiguriert."
                        )

                        return

                    if hmac.compare_digest(
                        entered_admin_code.strip(),
                        correct_admin_code,
                    ):
                        st.session_state[
                            "admin_granted"
                        ] = True

                        st.session_state[
                            "admin_dashboard_open"
                        ] = True

                        st.rerun()

                    else:
                        st.error(
                            "Admin-Code nicht korrekt."
                        )

                return

            st.success(
                "Admin angemeldet"
            )

            if st.button(
                "📊 Dashboard öffnen",
                use_container_width=True,
            ):
                st.session_state[
                    "admin_dashboard_open"
                ] = True

                st.rerun()

            if st.button(
                "Admin abmelden",
                use_container_width=True,
            ):
                st.session_state[
                    "admin_granted"
                ] = False

                st.session_state[
                    "admin_dashboard_open"
                ] = False

                st.rerun()

    if not st.session_state.get(
        "admin_granted",
        False,
    ):
        return

    if not st.session_state.get(
        "admin_dashboard_open",
        False,
    ):
        return

    try:
        rows = load_results()

    except psycopg.Error as exc:
        print(
            "Fehler beim Laden "
            "der Admin-Daten:",
            repr(exc),
        )

        st.error(
            "Die Ergebnisse konnten "
            "nicht geladen werden."
        )

        st.stop()

    render_admin_dashboard(
        rows
    )

    # Wenn das Dashboard offen ist, wird darunter
    # nicht zusätzlich die Teilnehmeransicht gerendert.
    st.stop()



# -------------------------------------------------------------------
# Eigene Drag&Drop-Komponente
# -------------------------------------------------------------------

COMPONENT_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">

<style>
    * {
        box-sizing: border-box;
    }

    html,
    body {
        margin: 0;
        padding: 0;

        width: 100%;

        background: transparent;

        font-family:
            -apple-system,
            BlinkMacSystemFont,
            "Segoe UI",
            Roboto,
            Helvetica,
            Arial,
            sans-serif;
    }

    body {
        overflow: visible;
        padding: 2px 1px 8px 1px;
    }

    /*
    Sechs Spalten über die verfügbare Breite.

    Die erste Spalte erhält etwas mehr Raum,
    weil dort anfangs alle 50 Karten liegen.
    */
    #board {
        display: grid;

        grid-template-columns:
            minmax(220px, 1.45fr)
            repeat(5, minmax(125px, 1fr));

        gap: 10px;

        width: 100%;

        align-items: start;
    }

    .column {
        min-width: 0;
        min-height: 110px;

        padding: 8px;

        border:
            1px solid
            rgba(128, 128, 128, 0.35);

        border-radius: 10px;

        background:
            rgba(128, 128, 128, 0.025);
    }

    .header {
        padding: 10px 6px;
        margin-bottom: 8px;

        border-radius: 7px;

        background:
            rgba(128, 128, 128, 0.16);

        font-weight: 700;
        text-align: center;

        line-height: 1.2rem;
    }

    /*
    Keine Scrollbox:
    Die Kategorie wächst mit ihrem Inhalt.
    */
    .dropzone {
        min-height: 70px;

        padding: 1px;

        border-radius: 7px;

        transition:
            background 0.10s ease,
            outline 0.10s ease;
    }

    .dropzone.drag-over {
        background:
            rgba(49, 130, 206, 0.06);

        outline:
            2px dashed
            rgba(49, 130, 206, 0.30);
    }

    /*
    Wichtig:
    Keine feste Kartenhöhe.
    Die Höhe ergibt sich ausschließlich
    aus der jeweiligen Textmenge.
    */
    .card {
        display: block;

        width: 100%;
        height: auto;
        min-height: 0;

        padding: 8px 10px;
        margin: 6px 0;

        border:
            1px solid
            rgba(128, 128, 128, 0.35);

        border-radius: 8px;

        background:
            rgba(128, 128, 128, 0.08);

        line-height: 1.3rem;

        white-space: normal;
        overflow-wrap: anywhere;
        word-break: normal;

        cursor: grab;

        user-select: none;

        transition:
            background 0.10s ease,
            opacity 0.10s ease,
            transform 0.08s ease;
    }

    .card:hover {
        background:
            rgba(128, 128, 128, 0.14);
    }

    .card:active {
        cursor: grabbing;
    }

    .card.dragging {
        opacity: 0.35;
    }

    /*
    Auf kleineren Bildschirmen darf die
    Komponente horizontal scrollen.
    Auf einem normalen Desktop werden alle
    sechs Spalten nebeneinander gezeigt.
    */
    @media (max-width: 1050px) {
        body {
            overflow-x: auto;
        }

        #board {
            min-width: 1000px;
        }
    }
</style>
</head>

<body>

<div id="board"></div>

<script>
    // -------------------------------------------------------------
    // Streamlit-Komponentenprotokoll
    // -------------------------------------------------------------

    function sendMessageToStreamlitClient(
        type,
        data = {}
    ) {
        const message = Object.assign(
            {
                isStreamlitMessage: true,
                type: type
            },
            data
        );

        window.parent.postMessage(
            message,
            "*"
        );
    }


    function componentReady() {
        sendMessageToStreamlitClient(
            "streamlit:componentReady",
            {
                apiVersion: 1
            }
        );
    }


    function setFrameHeight(height) {
        sendMessageToStreamlitClient(
            "streamlit:setFrameHeight",
            {
                height: height
            }
        );
    }


    function setComponentValue(value) {
        sendMessageToStreamlitClient(
            "streamlit:setComponentValue",
            {
                value: value
            }
        );
    }


    // -------------------------------------------------------------
    // Lokaler Zustand
    // -------------------------------------------------------------

    const boardElement =
        document.getElementById("board");

    let draggedCard = null;

    let originalParent = null;
    let originalNextSibling = null;

    let dropAccepted = false;

    let currentBoardJson = "";
    let lastReportedHeight = 0;


    // -------------------------------------------------------------
    // Iframe-Höhe an den tatsächlichen Inhalt anpassen
    // -------------------------------------------------------------

    function updateFrameHeight() {
    window.requestAnimationFrame(
        () => {
            /*
            Wichtig:
            Nicht document.scrollHeight messen,
            sondern nur die tatsächliche Höhe
            des Karten-Boards.

            Dadurch kann das Iframe nicht nur
            größer, sondern auch wieder kleiner
            werden.
            */
            const boardHeight =
                boardElement.getBoundingClientRect().height;

            const bodyStyle =
                window.getComputedStyle(
                    document.body
                );

            const paddingTop =
                parseFloat(
                    bodyStyle.paddingTop
                ) || 0;

            const paddingBottom =
                parseFloat(
                    bodyStyle.paddingBottom
                ) || 0;

            const height =
                Math.ceil(
                    boardHeight
                    + paddingTop
                    + paddingBottom
                    + 4
                );

            if (
                Math.abs(
                    height - lastReportedHeight
                ) > 1
            ) {
                lastReportedHeight =
                    height;

                setFrameHeight(
                    height
                );
            }
        }
    );
}


    // -------------------------------------------------------------
    // Aktuellen Board-Zustand aus dem DOM lesen
    // -------------------------------------------------------------

    function readBoard() {
        const result = [];

        document
            .querySelectorAll(".column")
            .forEach(
                column => {
                    const items = Array
                        .from(
                            column.querySelectorAll(
                                ".card"
                            )
                        )
                        .map(
                            card =>
                                card.dataset.value
                        );

                    result.push(
                        {
                            header:
                                column.dataset.header,

                            items:
                                items
                        }
                    );
                }
            );

        return result;
    }


    // -------------------------------------------------------------
    // Board zeichnen
    // -------------------------------------------------------------

    function renderBoard(board) {
        boardElement.innerHTML = "";

        board.forEach(
            container => {
                const column =
                    document.createElement(
                        "div"
                    );

                column.className = "column";

                column.dataset.header =
                    container.header;


                const header =
                    document.createElement(
                        "div"
                    );

                header.className = "header";

                header.textContent =
                    container.header;


                const zone =
                    document.createElement(
                        "div"
                    );

                zone.className = "dropzone";

                container.items.forEach(
                    value => {
                        const card =
                            document.createElement(
                                "div"
                            );

                        card.className =
                            "card";

                        card.draggable =
                            true;

                        card.dataset.value =
                            value;

                        /*
                        textContent ist absichtlich
                        kein innerHTML.
                        */
                        card.textContent =
                            value;

                        zone.appendChild(
                            card
                        );
                    }
                );

                column.appendChild(
                    header
                );

                column.appendChild(
                    zone
                );

                boardElement.appendChild(
                    column
                );
            }
        );

        currentBoardJson =
            JSON.stringify(board);

        updateFrameHeight();
    }


    // -------------------------------------------------------------
    // Position bestimmen, an der eine Karte eingefügt wird
    // -------------------------------------------------------------

    function getCardAfterPointer(
        zone,
        mouseY
    ) {
        const cards = [
            ...zone.querySelectorAll(
                ".card:not(.dragging)"
            )
        ];

        let closest = {
            offset:
                Number.NEGATIVE_INFINITY,

            element:
                null
        };

        cards.forEach(
            card => {
                const box =
                    card.getBoundingClientRect();

                const offset =
                    mouseY
                    - box.top
                    - box.height / 2;

                if (
                    offset < 0
                    && offset
                        > closest.offset
                ) {
                    closest = {
                        offset:
                            offset,

                        element:
                            card
                    };
                }
            }
        );

        return closest.element;
    }


    // -------------------------------------------------------------
    // Drag starten
    // -------------------------------------------------------------

    document.addEventListener(
        "dragstart",
        event => {
            const card =
                event.target.closest(
                    ".card"
                );

            if (!card) {
                return;
            }

            draggedCard =
                card;

            originalParent =
                card.parentElement;

            originalNextSibling =
                card.nextSibling;

            dropAccepted =
                false;

            event.dataTransfer.effectAllowed =
                "move";

            event.dataTransfer.setData(
                "text/plain",
                card.dataset.value
            );

            window.requestAnimationFrame(
                () => {
                    card.classList.add(
                        "dragging"
                    );
                }
            );
        }
    );


    // -------------------------------------------------------------
    // Karte während des Ziehens verschieben
    // -------------------------------------------------------------

    document.addEventListener(
        "dragover",
        event => {
            const zone =
                event.target.closest(
                    ".dropzone"
                );

            if (
                !zone
                || !draggedCard
            ) {
                return;
            }

            event.preventDefault();

            event.dataTransfer.dropEffect =
                "move";

            document
                .querySelectorAll(
                    ".dropzone"
                )
                .forEach(
                    z =>
                        z.classList.remove(
                            "drag-over"
                        )
                );

            zone.classList.add(
                "drag-over"
            );

            const afterElement =
                getCardAfterPointer(
                    zone,
                    event.clientY
                );

            if (
                afterElement === null
            ) {
                zone.appendChild(
                    draggedCard
                );
            } else {
                zone.insertBefore(
                    draggedCard,
                    afterElement
                );
            }
        }
    );


    // -------------------------------------------------------------
    // Karte erfolgreich ablegen
    // -------------------------------------------------------------

    document.addEventListener(
        "drop",
        event => {
            const zone =
                event.target.closest(
                    ".dropzone"
                );

            if (
                !zone
                || !draggedCard
            ) {
                return;
            }

            event.preventDefault();

            dropAccepted =
                true;
        }
    );


    // -------------------------------------------------------------
    // Drag abgeschlossen
    // -------------------------------------------------------------

    document.addEventListener(
        "dragend",
        () => {
            if (!draggedCard) {
                return;
            }

            draggedCard.classList.remove(
                "dragging"
            );

            document
                .querySelectorAll(
                    ".dropzone"
                )
                .forEach(
                    z =>
                        z.classList.remove(
                            "drag-over"
                        )
                );


            if (!dropAccepted) {
                /*
                Außerhalb einer Dropzone
                losgelassen:
                Karte an die alte Position
                zurücksetzen.
                */
                if (originalNextSibling) {
                    originalParent.insertBefore(
                        draggedCard,
                        originalNextSibling
                    );
                } else {
                    originalParent.appendChild(
                        draggedCard
                    );
                }
            } else {
                const newBoard =
                    readBoard();

                const newBoardJson =
                    JSON.stringify(
                        newBoard
                    );

                /*
                Nur dann an Python schicken,
                wenn sich tatsächlich etwas
                geändert hat.
                */
                if (
                    newBoardJson
                    !== currentBoardJson
                ) {
                    currentBoardJson =
                        newBoardJson;

                    setComponentValue(
                        newBoard
                    );
                }
            }


            draggedCard =
                null;

            originalParent =
                null;

            originalNextSibling =
                null;

            dropAccepted =
                false;


            updateFrameHeight();
        }
    );


    // -------------------------------------------------------------
    // Streamlit schickt Python-Argumente an die Komponente
    // -------------------------------------------------------------

    function onDataFromPython(event) {
        if (
            !event.data
            || event.data.type
                !== "streamlit:render"
        ) {
            return;
        }

        const args =
            event.data.args || {};

        const board =
            args.board || [];

        const incomingJson =
            JSON.stringify(
                board
            );

        /*
        Bei einem normalen Streamlit-Rerun
        wird nicht unnötig neu gerendert.
        */
        if (
            incomingJson
            !== currentBoardJson
        ) {
            renderBoard(
                board
            );
        } else {
            updateFrameHeight();
        }
    }


    window.addEventListener(
        "message",
        onDataFromPython
    );


    window.addEventListener(
        "resize",
        () => {
            updateFrameHeight();
        }
    );


    componentReady();
</script>

</body>
</html>
"""


def prepare_component():
    """
    Die Custom Component besteht aus einer einzigen HTML-Datei.
    Sie wird beim Start automatisch in das temporäre Verzeichnis
    geschrieben. Es ist daher keine zusätzliche Projektdatei nötig.
    """
    component_dir = (
        Path(tempfile.gettempdir())
        / "karteikarten_dragdrop_component_v1"
    )

    component_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    index_file = (
        component_dir
        / "index.html"
    )

    if (
        not index_file.exists()
        or index_file.read_text(
            encoding="utf-8"
        ) != COMPONENT_HTML
    ):
        index_file.write_text(
            COMPONENT_HTML,
            encoding="utf-8",
        )

    return component_dir


COMPONENT_DIR = prepare_component()

_card_sorter_component = (
    components.declare_component(
        "karteikarten_dragdrop_v1",
        path=str(COMPONENT_DIR),
    )
)


def card_sorter(board, key):
    return _card_sorter_component(
        board=board,
        key=key,
        default=board,
    )


# -------------------------------------------------------------------
# Streamlit-App
# -------------------------------------------------------------------

st.set_page_config(
    page_title="Karteikarten-Zuordnung",
    page_icon="🗂️",
    layout="wide",
)


# Möglichst viel Bildschirmbreite verwenden.
st.markdown(
    """
    <style>
        .block-container {
            max-width: 100% !important;
            padding-left: 1rem !important;
            padding-right: 1rem !important;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

def require_access_code():
    # Wenn der Zugang in dieser Sitzung schon freigeschaltet wurde,
    # muss der Code nicht erneut eingegeben werden.
    if st.session_state.get("access_granted", False):
        return

    st.title("🔐 Zugang zur Umfrage")

    st.write(
        "Bitte geben Sie den Zugangscode ein, "
        "um zu beginnen."
    )

    with st.form("access_form"):
        entered_code = st.text_input(
            "Zugangscode",
            type="password",
        )

        submitted = st.form_submit_button(
            "Weiter",
            type="primary",
        )

    if submitted:
        try:
            correct_code = str(
                st.secrets["ACCESS_CODE"]
            )
        except Exception:
            st.error(
                "Es wurde noch kein Zugangscode konfiguriert."
            )
            st.stop()

        if hmac.compare_digest(
            entered_code.strip(),
            correct_code,
        ):
            st.session_state.access_granted = True
            st.rerun()

        else:
            st.error(
                "Der Zugangscode ist nicht korrekt."
            )

    # Alles, was danach in app.py steht, wird erst ausgeführt,
    # wenn der richtige Code eingegeben wurde.
    st.stop()

require_access_code()

init_db()

render_admin_area()

if "board" not in st.session_state:
    st.session_state.board = (
        initial_board()
    )


if (
    "sorter_generation"
    not in st.session_state
):
    st.session_state.sorter_generation = 0


if "submitted" not in st.session_state:
    st.session_state.submitted = False


def start_new_entry():
    st.session_state.board = (
        initial_board()
    )

    st.session_state.sorter_generation += 1

    st.session_state.submitted = False

    st.session_state.participant_id = ""


st.title(
    "🗂️ Karteikarten-Zuordnung"
)

st.write(
    "Ordnen Sie die Deskriptoren dem Kompetenzniveau zu,  .“ "
    "in dem ein:e minimal kompetente:r Leser:in die beschriebene "
    " Leseleistung ohne Unterstützung zuverlässig erbringt."
    "**Pre-A1, A1, A2, B1 oder B2**."
)


participant_id = st.text_input(
    "Teilnehmer-ID",
    key="participant_id",
    placeholder="z. B. P001",
    help=(
        "Bitte keine Namen eingeben, "
        "wenn eine pseudonyme ID ausreicht."
    ),
    disabled=st.session_state.submitted,
)


if st.session_state.submitted:
    st.success(
        "Vielen Dank! Ihre Zuordnung wurde gespeichert. "
        "Sie können die Seite jetzt schließen."
    )

    st.button(
        "Neue Eingabe starten",
        type="secondary",
        on_click=start_new_entry,
    )

    st.stop()


st.caption(
    "Tipp: Ziehen Sie die Karten in die gewünschte Kategorie. "
    "Karten können jederzeit zwischen den Kategorien verschoben werden."
)


component_result = card_sorter(
    st.session_state.board,
    key=(
        "card_sorter_"
        f"{st.session_state.sorter_generation}"
    ),
)


# Wenn der Browser eine neue Zuordnung meldet,
# übernehmen wir sie und führen genau einen sauberen
# Streamlit-Rerun aus.
if (
    component_result
    != st.session_state.board
):
    if is_safe_board_payload(
        component_result
    ):
        st.session_state.board = (
            component_result
        )

        st.rerun()


status = validate_board(
    st.session_state.board
)

assigned = (
    len(CARDS)
    - status["unassigned_count"]
)


st.progress(
    assigned / len(CARDS)
)

st.write(
    f"**{assigned} von "
    f"{len(CARDS)} Karten zugeordnet.**"
)


if (
    status["unassigned_count"]
    > 0
):
    st.info(
        f"Noch "
        f"{status['unassigned_count']} "
        "Karte(n) nicht zugeordnet."
    )

elif status["is_valid"]:
    st.success(
        "Alle 50 Karten sind vollständig "
        "und eindeutig zugeordnet."
    )

else:
    st.error(
        "Die Kartenstruktur ist inkonsistent. "
        "Bitte setzen Sie die Zuordnung zurück."
    )


button_col, reset_col = st.columns(
    [2, 1]
)


with button_col:
    submit = st.button(
        "Ergebnis absenden",
        type="primary",
        use_container_width=True,
        disabled=not status["is_valid"],
    )


with reset_col:
    reset = st.button(
        "Zuordnung zurücksetzen",
        use_container_width=True,
    )


if reset:
    st.session_state.board = (
        initial_board()
    )

    st.session_state.sorter_generation += 1

    st.rerun()


if submit:
    clean_participant_id = (
        participant_id.strip()
    )

    if not clean_participant_id:
        st.error(
            "Bitte geben Sie zuerst "
            "eine Teilnehmer-ID ein."
        )

    elif (
        len(clean_participant_id)
        > 100
    ):
        st.error(
            "Die Teilnehmer-ID darf "
            "höchstens 100 Zeichen lang sein."
        )

    else:
        assignments = (
            board_to_assignments(
                st.session_state.board
            )
        )

        if (
            len(assignments)
            != len(CARDS)
        ):
            st.error(
                "Es konnten nicht alle "
                "50 Zuordnungen gelesen werden. "
                "Bitte prüfen Sie die Karten."
            )

        else:
            try:
                save_submission(
                    clean_participant_id,
                    assignments,
                )

            except psycopg.errors.UniqueViolation:
                st.error(
                    "Diese Teilnehmer-ID wurde "
                    "bereits verwendet. "
                    "Bitte prüfen Sie die ID "
                    "oder verwenden Sie eine andere."
                )
            
            except psycopg.Error as exc:
                # Technische Details nur im Streamlit-Log ausgeben.
                print(
                    "PostgreSQL-Datenbankfehler:",
                    repr(exc),
                )
            
                st.error(
                    "Beim Speichern ist ein Datenbankfehler "
                    "aufgetreten. Bitte versuchen Sie es erneut."
                )

            else:
                st.session_state.submitted = True

                st.rerun()
