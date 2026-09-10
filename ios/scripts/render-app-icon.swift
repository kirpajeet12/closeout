#!/usr/bin/env swift

import AppKit
import ImageIO
import UniformTypeIdentifiers

// Closeout app icon: a sheet from the drawing set on an ink ground, stamped closed.
// Cream paper, faint drafting grid, a folded corner, and a safety-orange ring with a check.
// Run: swift ios/scripts/render-app-icon.swift [preview.png]
// iOS applies the rounded mask; the source has square, opaque edges on purpose.
let iosDirectory = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
let iconURL = iosDirectory.appendingPathComponent("Closeout/Assets.xcassets/AppIcon.appiconset/AppIcon.png")
let previewURL = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/dev/null")
let size = 1024
let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
let bitmapInfo = CGBitmapInfo.byteOrder32Big.rawValue | CGImageAlphaInfo.noneSkipLast.rawValue

func rgb(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> CGColor {
    CGColor(colorSpace: colorSpace, components: [r / 255, g / 255, b / 255, a])!
}
func gradient(_ colors: [CGColor], _ locations: [CGFloat]) -> CGGradient {
    CGGradient(colorsSpace: colorSpace, colors: colors as CFArray, locations: locations)!
}

guard let ctx = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8,
                          bytesPerRow: size * 4, space: colorSpace, bitmapInfo: bitmapInfo)
else { fatalError("Could not create the icon canvas.") }
ctx.setAllowsAntialiasing(true)
ctx.setShouldAntialias(true)
ctx.interpolationQuality = .high
ctx.translateBy(x: 0, y: CGFloat(size))
ctx.scaleBy(x: 1, y: -1)
let S = CGFloat(size)

// 1. Ground: ink, with a faint warm lift toward the top-left.
ctx.drawLinearGradient(gradient([rgb(38, 39, 43), rgb(18, 19, 20)], [0, 1]),
                       start: CGPoint(x: 0, y: 0), end: CGPoint(x: S, y: S), options: [])

// 2. The sheet: cream paper, slightly turned, with a soft shadow.
let sheet = CGRect(x: 150, y: 168, width: 724, height: 724)
ctx.saveGState()
ctx.translateBy(x: sheet.midX, y: sheet.midY)
ctx.rotate(by: -4 * .pi / 180)
ctx.translateBy(x: -sheet.midX, y: -sheet.midY)
ctx.setShadow(offset: CGSize(width: 0, height: -18), blur: 48, color: rgb(0, 0, 0, 0.55))
ctx.setFillColor(rgb(247, 244, 237))
ctx.fill(sheet)
ctx.setShadow(offset: .zero, blur: 0, color: nil)

// Drafting grid on the sheet.
ctx.setStrokeColor(rgb(211, 205, 191, 0.9))
ctx.setLineWidth(2)
var x = sheet.minX + 52
while x < sheet.maxX {
    ctx.move(to: CGPoint(x: x, y: sheet.minY)); ctx.addLine(to: CGPoint(x: x, y: sheet.maxY)); x += 52
}
var y = sheet.minY + 52
while y < sheet.maxY {
    ctx.move(to: CGPoint(x: sheet.minX, y: y)); ctx.addLine(to: CGPoint(x: sheet.maxX, y: y)); y += 52
}
ctx.strokePath()

// Title block strip along the bottom of the sheet, like a real drawing.
ctx.setFillColor(rgb(18, 19, 20))
ctx.fill(CGRect(x: sheet.minX + 40, y: sheet.maxY - 96, width: 300, height: 14))
ctx.fill(CGRect(x: sheet.minX + 40, y: sheet.maxY - 66, width: 180, height: 14))
ctx.setStrokeColor(rgb(18, 19, 20))
ctx.setLineWidth(6)
ctx.stroke(sheet.insetBy(dx: 22, dy: 22))

// Folded corner, top-right.
let fold: CGFloat = 132
ctx.setFillColor(rgb(38, 39, 43))
ctx.move(to: CGPoint(x: sheet.maxX - fold, y: sheet.minY))
ctx.addLine(to: CGPoint(x: sheet.maxX, y: sheet.minY))
ctx.addLine(to: CGPoint(x: sheet.maxX, y: sheet.minY + fold))
ctx.closePath(); ctx.fillPath()
ctx.setFillColor(rgb(230, 225, 214))
ctx.move(to: CGPoint(x: sheet.maxX - fold, y: sheet.minY))
ctx.addLine(to: CGPoint(x: sheet.maxX - fold, y: sheet.minY + fold))
ctx.addLine(to: CGPoint(x: sheet.maxX, y: sheet.minY + fold))
ctx.closePath(); ctx.fillPath()
ctx.restoreGState()

// 3. The stamp: safety-orange ring with a check, turned the other way so it reads as stamped on.
let centre = CGPoint(x: 560, y: 520)
ctx.saveGState()
ctx.translateBy(x: centre.x, y: centre.y)
ctx.rotate(by: 9 * .pi / 180)
ctx.setStrokeColor(rgb(232, 89, 12))
ctx.setLineWidth(46)
ctx.setLineCap(.round)
ctx.setLineJoin(.round)
ctx.strokeEllipse(in: CGRect(x: -215, y: -215, width: 430, height: 430))
ctx.setLineWidth(58)
ctx.move(to: CGPoint(x: -118, y: 6))
ctx.addLine(to: CGPoint(x: -28, y: 96))
ctx.addLine(to: CGPoint(x: 132, y: -92))
ctx.strokePath()
ctx.restoreGState()

guard let image = ctx.makeImage() else { fatalError("Could not render the icon.") }
for url in [iconURL, previewURL] where url.path != "/dev/null" {
    try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    guard let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil)
    else { fatalError("Could not write \(url.path)") }
    CGImageDestinationAddImage(dest, image, nil)
    CGImageDestinationFinalize(dest)
}
print("Wrote \(iconURL.path)")
