/**
 * 悬壶 WebUI —— Ant Design 主题令牌
 *
 * 将 UI 设计文档 §2 色板映射到 Ant Design v6 的 token。
 */

import type { ThemeConfig } from 'antd'

export const xuanhuTheme: ThemeConfig = {
  token: {
    colorPrimary: '#C04040',
    colorLink: '#3D5A4B',
    colorSuccess: '#5B8C5A',
    colorWarning: '#C49B3C',
    colorError: '#A8443A',
    colorInfo: '#6B7D8A',
    colorTextBase: '#3C3228',
    colorBgLayout: '#FDF8F0',
    colorBgContainer: '#F5EDDF',
    borderRadius: 8,
    borderRadiusLG: 8,
    borderRadiusSM: 4,
    fontFamily: '"Noto Sans SC", "Inter", system-ui, "Microsoft YaHei", sans-serif',
    fontSize: 14,
    controlHeight: 36,
    wireframe: false,
  },
  components: {
    Layout: {
      siderBg: '#F5EDDF',
      headerBg: '#FDF8F0',
      bodyBg: '#FDF8F0',
    },
    Card: {
      colorBgContainer: '#F5EDDF',
      boxShadowTertiary: '0 1px 3px rgba(60, 50, 40, 0.06)',
    },
    Button: {
      borderRadius: 6,
    },
    Input: {
      borderRadius: 6,
    },
    Steps: {
      colorPrimary: '#C04040',
    },
  },
}
