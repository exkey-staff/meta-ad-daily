// バナー画像から文字と位置を読む（macOS標準のVisionを使う。ネットに出さない）
// 使い方: jpocr <画像> [<画像> ...]
// 出力: 1画像1行のJSON（path, width, height, lines[{text, conf, x, y, w, h}]）
//       x,y,w,h は画像に対する割合（0〜1）。y は上からの距離。
import Foundation
import Vision
import AppKit

func recognize(_ path: String) -> [String: Any] {
    guard let img = NSImage(contentsOfFile: path),
          let tiff = img.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff),
          let cg = rep.cgImage else {
        return ["path": path, "error": "画像を開けない"]
    }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["ja-JP", "en-US"]
    req.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do { try handler.perform([req]) } catch {
        return ["path": path, "error": "\(error)"]
    }
    var lines: [[String: Any]] = []
    for obs in (req.results ?? []) {
        guard let top = obs.topCandidates(1).first else { continue }
        let b = obs.boundingBox   // 左下原点
        lines.append([
            "text": top.string,
            "conf": Double(round(1000 * top.confidence) / 1000),
            "x": Double(round(10000 * b.minX) / 10000),
            "y": Double(round(10000 * (1 - b.maxY)) / 10000),
            "w": Double(round(10000 * b.width) / 10000),
            "h": Double(round(10000 * b.height) / 10000),
        ])
    }
    // 上から下、同じ高さなら左から右に並べ直す
    lines.sort {
        let a = $0["y"] as! Double, b = $1["y"] as! Double
        if abs(a - b) > 0.02 { return a < b }
        return ($0["x"] as! Double) < ($1["x"] as! Double)
    }
    return ["path": path, "width": rep.pixelsWide, "height": rep.pixelsHigh, "lines": lines]
}

let args = Array(CommandLine.arguments.dropFirst())
if args.isEmpty {
    FileHandle.standardError.write("使い方: jpocr <画像> [<画像> ...]\n".data(using: .utf8)!)
    exit(2)
}
for path in args {
    let out = recognize(path)
    if let data = try? JSONSerialization.data(withJSONObject: out, options: []),
       let s = String(data: data, encoding: .utf8) {
        print(s)
    }
}
