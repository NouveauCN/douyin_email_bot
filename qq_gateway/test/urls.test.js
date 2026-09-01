import assert from "node:assert/strict";
import test from "node:test";
import { extractSupportedUrl, extractSupportedUrls } from "../src/urls.js";

test("extracts one supported Douyin or Bilibili URL", () => {
  assert.equal(extractSupportedUrl("请下载 https://v.douyin.com/abc_1/。"), "https://v.douyin.com/abc_1/");
  assert.equal(extractSupportedUrl("https://www.douyin.com/video/12345"), "https://www.douyin.com/video/12345");
  assert.equal(extractSupportedUrl("https://www.bilibili.com/video/BV1xx"), "https://www.bilibili.com/video/BV1xx");
});

test("returns all links in message order", () => {
  assert.deepEqual(extractSupportedUrls("https://b23.tv/x 和 https://v.douyin.com/y"), [
    "https://b23.tv/x",
    "https://v.douyin.com/y",
  ]);
  assert.deepEqual(extractSupportedUrls("没有链接"), []);
});
