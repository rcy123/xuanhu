/**
 * 悬壶 WebUI —— 文件下载工具
 *
 * 处理非 envelope 文件响应：解析 Content-Disposition（含 RFC 5987
 * filename*），通过 Blob + createObjectURL + <a download> 触发浏览器下载。
 */

/**
 * 解析 Content-Disposition 响应头，提取文件名。
 *
 * 支持格式：
 * - Content-Disposition: attachment; filename="病历_abc_20260704.txt"
 * - Content-Disposition: attachment; filename*=UTF-8''%E7%97%85%E5%8E%86_abc.txt
 *
 * 优先使用 filename*（RFC 5987），其次使用 filename。
 */
function parseFilename(header: string | null): string | null {
  if (!header) return null

  // 优先解析 filename*（RFC 5987）
  const starMatch = header.match(/filename\*=UTF-8''([^;]+)/i)
  if (starMatch) {
    try {
      return decodeURIComponent(starMatch[1])
    } catch {
      // fall through to filename
    }
  }

  // 解析 filename
  const match = header.match(/filename="([^"]+)"/i)
  if (match) return match[1]

  // 无引号
  const match2 = header.match(/filename=([^;]+)/i)
  if (match2) return match2[1].trim()

  return null
}

/**
 * 触发浏览器下载文件（非 envelope 响应）。
 *
 * @param response - fetch 的 raw Response
 * @param fallbackName - 解析不到文件名时的回退名（不含扩展名），如 "病历"
 * @param fallbackExt - 回退扩展名，如 "txt" | "json" | "md"
 */
export async function downloadFileResponse(
  response: Response,
  fallbackName: string,
  fallbackExt: string,
): Promise<void> {
  const contentDisposition = response.headers.get('content-disposition')
  const filename = parseFilename(contentDisposition) ?? `${fallbackName}.${fallbackExt}`

  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  // 延迟释放以便浏览器完成下载
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}