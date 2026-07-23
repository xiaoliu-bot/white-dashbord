# 收款码图片目录 / QR code assets

开屏赞赏海报（`donate.html`）会引用本目录下的两张收款码图片：

- `wechat-pay-cropped.png` —— 微信赞赏码 / 微信收款码
- `alipay.jpg` —— 支付宝收款码

## 放置方式
把你自己微信 / 支付宝的收款码截图，按上面的文件名放进本目录：

```
assets/wechat-pay-cropped.png
assets/alipay.jpg
```

## 说明
- 图片缺失时，`donate.html` 已内置优雅占位（显示「收款码图片缺失 / 请放入 assets/...」），不会破图。
- 文件名需严格一致（区分大小写）。
- 建议尺寸：正方形、清晰即可，海报区会自适应裁切为 220×220 圆角卡片。
