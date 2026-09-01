const DOUYIN = /https?:\/\/(?:v\.douyin\.com\/[A-Za-z0-9_-]+\/?|www\.douyin\.com\/(?:video|note)\/\d+)/giu;
const BILIBILI = /https?:\/\/(?:(?:www|m)\.bilibili\.com\/\S+|b23\.tv\/\S+)/giu;
const TRAILING = /[\s>）\)\]}，。！？、；;,.!?]+$/u;

export function cleanUrl(value) {
  return String(value).replace(TRAILING, "");
}

export function extractSupportedUrls(text) {
  const input = String(text ?? "");
  const matches = [...input.matchAll(DOUYIN), ...input.matchAll(BILIBILI)]
    .map((match) => ({ index: match.index ?? 0, url: cleanUrl(match[0]) }))
    .filter(({ url }) => url.length > 0)
    .sort((a, b) => a.index - b.index)
    .map(({ url }) => url);
  return [...new Set(matches)];
}

export function extractSupportedUrl(text) {
  return extractSupportedUrls(text)[0] ?? null;
}
