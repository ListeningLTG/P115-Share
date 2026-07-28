/**
 * 复制文本到剪贴板，支持 HTTP (非 secureContext) 环境下的降级兼容
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (!text) return false;

  // 1. 优先尝试 Modern Clipboard API（仅在 HTTPS 或 localhost 下有效）
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      console.warn('navigator.clipboard.writeText 失败，尝试 fallback 方案:', err);
    }
  }

  // 2. 降级方案：创建隐藏 textarea 使用 document.execCommand('copy')
  try {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    // 防止页面滚动和可见
    textarea.style.position = 'fixed';
    textarea.style.top = '-999999px';
    textarea.style.left = '-999999px';
    textarea.style.opacity = '0';
    textarea.setAttribute('readonly', '');
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, 999999);

    const successful = document.execCommand('copy');
    document.body.removeChild(textarea);
    return successful;
  } catch (err) {
    console.error('execCommand copy 失败:', err);
    return false;
  }
}
