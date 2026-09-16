import json
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components


DB_PATH = Path(__file__).with_name("survey.db")

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

def get_connection():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=20,
    )

    conn.execute(
        "PRAGMA foreign_keys = ON;"
    )

    conn.execute(
        "PRAGMA journal_mode = WAL;"
    )

    return conn


def init_db():
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                submission_id TEXT PRIMARY KEY,
                participant_id TEXT NOT NULL UNIQUE,
                submitted_at_utc TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assignments (
                submission_id TEXT NOT NULL,
                card_id TEXT NOT NULL,
                category TEXT NOT NULL,

                PRIMARY KEY (
                    submission_id,
                    card_id
                ),

                FOREIGN KEY (
                    submission_id
                )
                REFERENCES submissions(
                    submission_id
                )
                ON DELETE CASCADE
            )
            """
        )


def save_submission(
    participant_id,
    assignments,
):
    submission_id = str(
        uuid.uuid4()
    )

    submitted_at = (
        datetime.now(timezone.utc)
        .isoformat()
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
        conn.execute(
            """
            INSERT INTO submissions (
                submission_id,
                participant_id,
                submitted_at_utc
            )
            VALUES (?, ?, ?)
            """,
            (
                submission_id,
                participant_id,
                submitted_at,
            ),
        )

        conn.executemany(
            """
            INSERT INTO assignments (
                submission_id,
                card_id,
                category
            )
            VALUES (?, ?, ?)
            """,
            rows,
        )

    return submission_id


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


init_db()


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
    "Ordnen Sie bitte jede der 50 Karten "
    "per Drag & Drop genau einer Kategorie zu: "
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

            except sqlite3.IntegrityError:
                st.error(
                    "Diese Teilnehmer-ID wurde "
                    "bereits verwendet. "
                    "Bitte prüfen Sie die ID "
                    "oder verwenden Sie eine andere."
                )

            except sqlite3.Error as exc:
                st.error(
                    "Beim Speichern ist ein "
                    "Datenbankfehler aufgetreten. "
                    f"Details: {exc}"
                )

            else:
                st.session_state.submitted = True

                st.rerun()
