/**
 * 悬壶 WebUI —— Ant Design 主题令牌
 *
 * 将 UI 设计文档 §2 色板映射到 Ant Design v6 的 token。
 */

import type { ThemeConfig } from 'antd'

export const xuanhuTheme: ThemeConfig = {
  token: {
    colorPrimary: '#8E3F38',
    colorLink: '#2F6652',
    colorSuccess: '#40785C',
    colorWarning: '#A97824',
    colorError: '#A6443D',
    colorInfo: '#557584',
    colorTextBase: '#262A26',
    colorTextSecondary: '#6F746E',
    colorBorder: '#DDD8CD',
    colorBorderSecondary: '#EAE6DE',
    colorBgLayout: '#F2F1EC',
    colorBgContainer: '#FFFEFA',
    colorBgElevated: '#FFFEFA',
    borderRadius: 10,
    borderRadiusLG: 14,
    borderRadiusSM: 8,
    fontFamily: '"Noto Sans SC", "Inter", system-ui, "Microsoft YaHei", sans-serif',
    fontSize: 14,
    controlHeight: 40,
    wireframe: false,
  },
  components: {
    Layout: {
      siderBg: '#F8F5EE',
      headerBg: '#FFFEFA',
      bodyBg: '#F2F1EC',
    },
    Card: {
      colorBgContainer: '#FFFEFA',
      boxShadowTertiary: '0 8px 24px rgba(42, 39, 34, 0.06)',
    },
    Button: {
      borderRadius: 10,
      primaryShadow: '0 6px 18px rgba(142, 63, 56, 0.18)',
    },
    Input: {
      borderRadius: 10,
    },
    Steps: {
      colorPrimary: '#C04040',
    },
  },
}
