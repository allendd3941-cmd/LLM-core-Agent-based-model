from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
from langchain_text_splitters import RecursiveCharacterTextSplitter


def RAG(user_prompt, data):
    splitter = RecursiveCharacterTextSplitter(
    chunk_size=50,
    chunk_overlap=10,
    separators=["\n\n", "\n", "。", "，", " ", ""]
    )

    chunks = splitter.split_text(data)
    vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(2, 4))
    vectors = vectorizer.fit_transform([user_prompt] + chunks) #再回來確認data格式，需要的是list of strings

    user_vector = vectors[0:1]
    data_vectors = vectors[1:]

    similarities = cosine_similarity(user_vector, data_vectors)[0]

    top_k = 3
    top_k_indices = np.argsort(similarities)[::-1][:top_k]

    retrieved_texts = []

    for rank, idx in enumerate(top_k_indices, start=1):
        print(f"RAG info:")
        print(f"Rank {rank}")
        print(f"Index: {idx}")
        print(f"Score: {similarities[idx]:.4f}")
        print()

        retrieved_texts.append(
            f"[資料 {rank} | 相似度 {similarities[idx]:.4f}]\n{chunks[idx]}"
        )

    return "\n\n".join(retrieved_texts)
