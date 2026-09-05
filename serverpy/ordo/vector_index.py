"""Small deterministic HNSW projection for the local baseline; exact search remains available."""
import hashlib
import heapq
import math


def build(chunks, m=12, ef=64):
    from .knowledge import local_embedding, cosine
    vectors, layers, entry, maximum = {}, [], None, -1
    def distance(a, b):
        return 1 - cosine(vectors[a], vectors[b])
    def search(query_id, start, layer, width):
        visited, pending, best = {start}, [(distance(query_id, start), start)], [(-distance(query_id, start), start)]
        while pending:
            dist, node = heapq.heappop(pending)
            if len(best) >= width and dist > -best[0][0]:
                break
            for neighbor in layers[layer].get(node, []):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                score = distance(query_id, neighbor)
                if len(best) < width or score < -best[0][0]:
                    heapq.heappush(pending, (score, neighbor))
                    heapq.heappush(best, (-score, neighbor))
                    if len(best) > width:
                        heapq.heappop(best)
        return [node for _, node in sorted([(-d, node) for d, node in best])]
    for chunk in sorted(chunks, key=lambda item: item['id']):
        node = chunk['id']
        vectors[node] = chunk.get('embedding') or local_embedding(chunk['content_text'])
        uniform = (int.from_bytes(hashlib.sha256(node.encode()).digest()[:8], 'big') + 1) / (2 ** 64 + 1)
        level = min(16, int(-math.log(uniform) / math.log(m)))
        while len(layers) <= level:
            layers.append({})
        for layer in range(level + 1):
            layers[layer][node] = []
        if entry is None:
            entry, maximum = node, level
            continue
        nearest = entry
        for layer in range(maximum, level, -1):
            nearest = search(node, nearest, layer, 1)[0]
        for layer in range(min(level, maximum), -1, -1):
            neighbors = search(node, nearest, layer, ef)[:m]
            layers[layer][node] = neighbors
            for neighbor in neighbors:
                links = layers[layer][neighbor] + [node]
                layers[layer][neighbor] = sorted(set(links), key=lambda item: distance(neighbor, item))[:m * (2 if layer == 0 else 1)]
            if neighbors:
                nearest = neighbors[0]
        if level > maximum:
            entry, maximum = node, level
    return {'algorithm': 'HNSW', 'm': m, 'efConstruction': ef, 'entryPoint': entry, 'maxLevel': maximum, 'layers': layers, 'vectors': vectors}
