const cache = new Map();

export function sx(source = '') {
  if (cache.has(source)) return cache.get(source);
  const value = {};
  for (const declaration of source.split(';')) {
    const separator = declaration.indexOf(':');
    if (separator < 0) continue;
    const rawKey = declaration.slice(0, separator).trim();
    const rawValue = declaration.slice(separator + 1).trim();
    if (!rawKey || !rawValue) continue;
    const key = rawKey.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    value[key] = rawValue;
  }
  cache.set(source, value);
  return value;
}
