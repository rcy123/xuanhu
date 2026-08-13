/**
 * 悬壶 WebUI —— Ant Design 主题令牌
 *
 * 与 src/styles/tokens.css 共享同一套色板。
 * 调色板锚定 logo.png（葫芦水墨 + 叶片松针绿 + 朱砂方印）。
 */

import type { ThemeConfig } from 'antd'

export const xuanhuTheme: ThemeConfig = {
  token: {
    colorPrimary: '#C8442C',
    colorLink: '#6F8256',
    colorSuccess: '#5C7F4F',
    colorWarning: '#B58032',
    colorError: '#B04139',
    colorInfo: '#5F7488',
    colorTextBase: '#1B1A17',
    colorTextSecondary: '#5A5751',
    colorTextTertiary: '#8A8679',
    colorBorder: '#DCD4BF',
    colorBorderSecondary: '#E5DDC8',
    colorBgLayout: '#F2EDE0',
    colorBgContainer: '#FFFEFA',
    colorBgElevated: '#FFFEFA',
    borderRadius: 10,
    borderRadiusLG: 16,
    borderRadiusSM: 8,
    fontFamily:
      '"Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
    fontSize: 14,
    controlHeight: 40,
    wireframe: false,
    // 字号阶梯
    fontSizeXL: 18,
    fontSizeHeading1: 32,
    fontSizeHeading2: 26,
    fontSizeHeading3: 20,
    fontSizeHeading4: 17,
    fontSizeHeading5: 15,
  },
  components: {
    Layout: {
      siderBg: '#F5F0E2',
      headerBg: '#FFFEFA',
      bodyBg: '#F2EDE0',
    },
    Card: {
      colorBgContainer: '#FFFEFA',
      boxShadowTertiary:
        '0 1px 0 rgba(27, 26, 23, 0.04), 0 8px 24px rgba(45, 38, 28, 0.06)',
    },
    Button: {
      borderRadius: 10,
      borderRadiusLG: 12,
      primaryShadow: '0 6px 18px rgba(200, 68, 44, 0.18)',
      controlHeight: 40,
      fontWeight: 500,
    },
    Input: {
      borderRadius: 10,
      colorBgContainer: '#FFFEFA',
      activeShadow: '0 0 0 3px rgba(200, 68, 44, 0.16)',
    },
    Typography: {
      titleMarginTop: '1.2em',
      titleMarginBottom: '0.6em',
    },
    Steps: {
      colorPrimary: '#C8442C',
    },
    Alert: {
      borderRadiusLG: 12,
    },
  },
}