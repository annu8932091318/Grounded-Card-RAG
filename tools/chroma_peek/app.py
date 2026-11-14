"""Chroma Peek for this project — a ChromaDB 1.x-compatible take on github.com/Pawandeep-prog/chroma-peek.

Run (from the project root, venv active):
    pip install -r tools/chroma_peek/requirements.txt
    streamlit run tools/chroma_peek/app.py
Then open http://localhost:8501 . The persist path defaults to CHROMA_PATH from .env (./chroma_data).

What you see:
  * every collection (policy_chunks = searchable, policy_quarantine = never searched) with its row count
  * a table of the stored chunks: id, doc, section, version, authority, quarantine reason, token count,
    embedding dimension, the first embedding values and the text
  * a 2-D map of the embeddings (PCA), coloured by document: sections of one policy should cluster
  * a similarity query that embeds your text with the SAME model the agent uses (fastembed MiniLM),
    so the ranking matches the retriever's vector leg exactly

WHY not the upstream chroma-peek as is: with chromadb >= 1.x, collection.get() returns an "included" list
next to the per-row lists, so its pd.DataFrame(collection.get()) raises "All arrays must be of the same length",
and it calls st.experimental_rerun, which current Streamlit removed.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))  # so `streamlit run tools/chroma_peek/app.py` can import src.*

import chromadb  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from chromadb.config import Settings  # noqa: E402

from src.const.config import config  # noqa: E402

st.set_page_config(page_title="Chroma Peek · Grounded Card Agent", page_icon="👀", layout="wide")
st.title("Chroma Peek 👀")
st.caption("What the ingest pipeline embedded into ChromaDB: chunks, metadata, vectors, and live similarity search.")

path = st.text_input("Persist path", value=str(config.chroma_path), help="The CHROMA_PATH folder (default ./chroma_data)")
if st.button("🔄 Reload"):
    st.cache_resource.clear()
    st.rerun()


@st.cache_resource
def get_client(p: str):
    return chromadb.PersistentClient(path=p, settings=Settings(anonymized_telemetry=False))


@st.cache_resource
def get_embedder():
    from src.modules.retrieval.embedder import embed_query

    return embed_query


if not path or not Path(path).exists():
    st.warning("Path does not exist. Run `python -m scripts.ingest` first, then reload.")
    st.stop()

client = get_client(path)
collections = client.list_collections()
if not collections:
    st.warning("No collections yet. Run `python -m scripts.ingest`.")
    st.stop()

left, right = st.columns([1, 3])
with left:
    names = sorted((c.name for c in collections), key=lambda n: (n != config.chroma_collection, n))  # searchable first
    name = st.radio("Collection", names, format_func=lambda n: f"{n} ({client.get_collection(n).count()})")
    col = client.get_collection(name)
    st.json(col.metadata or {}, expanded=False)

got = col.get(include=["documents", "metadatas", "embeddings"])
embeddings = got["embeddings"]
rows = []
for i, (cid, doc, meta) in enumerate(zip(got["ids"], got["documents"], got["metadatas"], strict=True)):
    vec = embeddings[i] if embeddings is not None else []
    rows.append({
        "id": cid, "doc_id": meta.get("doc_id"), "section": meta.get("section"), "version": meta.get("version"),
        "authority": meta.get("authority"), "quarantine_reason": meta.get("quarantine_reason") or "",
        "tokens": meta.get("token_count"), "dim": len(vec),
        "embedding[:4]": ", ".join(f"{float(v):+.3f}" for v in vec[:4]), "text": doc,
    })
df = pd.DataFrame(rows)

with right:
    st.markdown(f"**{len(df)} chunks** in `{name}`")
    docs = sorted(df["doc_id"].dropna().unique()) if len(df) else []
    picked = st.multiselect("Filter by document", docs)
    view = df[df["doc_id"].isin(picked)] if picked else df
    st.dataframe(view, width="stretch", height=360, hide_index=True)

st.divider()
st.subheader("Embedding map (PCA → 2-D)")
if embeddings is not None and len(embeddings) >= 3:
    x = np.asarray(embeddings, dtype=float)
    x = x - x.mean(axis=0)
    _, _, vt = np.linalg.svd(x, full_matrices=False)  # PCA via SVD: no scikit-learn needed
    xy = x @ vt[:2].T
    st.scatter_chart(pd.DataFrame({"pc1": xy[:, 0], "pc2": xy[:, 1], "doc": df["doc_id"]}), x="pc1", y="pc2", color="doc", height=420)
    st.caption("Each dot is one chunk (one policy section). Nearby dots have similar meaning.")
else:
    st.info("Need at least 3 vectors to draw the map.")

st.divider()
st.subheader("Similarity query")
query = st.text_input("Question", placeholder="e.g. what is the foreign currency markup?")
k = st.slider("Results", 1, 10, 5)
if query and col.count():
    from src.helper.text import normalise

    vec = get_embedder()(normalise(query))  # same normalisation + model as the agent's retriever
    res = col.query(query_embeddings=[vec], n_results=min(k, col.count()), include=["documents", "metadatas", "distances"])
    out = pd.DataFrame({
        "similarity": [round(1 - d, 4) for d in res["distances"][0]],
        "doc_id": [m["doc_id"] for m in res["metadatas"][0]],
        "section": [m["section"] for m in res["metadatas"][0]],
        "text": res["documents"][0],
    })
    st.dataframe(out, width="stretch", hide_index=True)
    st.caption(f"The agent's relevance gate treats similarity ≥ {config.retrieval_min_vector_sim} as a semantic match "
               "(or a specific BM25 keyword hit — see /api/v1/retrieval/search for both legs).")
