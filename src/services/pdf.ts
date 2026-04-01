import * as pdfjsLib from 'pdfjs-dist'
import { dataUrlToBytes } from './utils'

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url,
).toString()

export async function extractPdfText(dataUrl: string): Promise<string> {
  const bytes = dataUrlToBytes(dataUrl)
  if (bytes.length === 0) return ''

  const doc = await pdfjsLib.getDocument({ data: bytes }).promise
  const pages: string[] = []

  for (let i = 1; i <= doc.numPages; i++) {
    const page = await doc.getPage(i)
    const content = await page.getTextContent()
    const text = content.items
      .map((item: any) => item.str)
      .join('')
    if (text.trim()) {
      pages.push('--- 第 ' + i + ' 页 ---\n' + text)
    }
  }

  return pages.join('\n\n')
}

const PAGE_SCALE = 1.5
const JPEG_QUALITY = 0.6

export async function renderPdfToImages(
  dataUrl: string,
): Promise<string[]> {
  const bytes = dataUrlToBytes(dataUrl)
  if (bytes.length === 0) return []

  const doc = await pdfjsLib.getDocument({ data: bytes }).promise
  const images: string[] = []

  for (let i = 1; i <= doc.numPages; i++) {
    const page = await doc.getPage(i)
    const viewport = page.getViewport({ scale: PAGE_SCALE })

    const canvas = document.createElement('canvas')
    canvas.width = viewport.width
    canvas.height = viewport.height
    const ctx = canvas.getContext('2d')!

    await page.render({ canvasContext: ctx, viewport }).promise
    images.push(canvas.toDataURL('image/jpeg', JPEG_QUALITY))

    canvas.width = 0
    canvas.height = 0
  }

  return images
}
