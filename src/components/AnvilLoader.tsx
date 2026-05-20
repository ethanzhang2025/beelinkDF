// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
//
// 智能问数加载层：4 张抽象图表小卡片（柱 / 折线 / 环 / 表格网格），
// 浅色 + 半透明背景 + 轻浮动 / shimmer，配合中文文案
// 「正在加载智能分析工作台...」。纯 SVG + CSS，无图片资源 / 无新依赖。

import React from 'react';
import { Box, Typography, alpha } from '@mui/material';
import { keyframes } from '@mui/system';

const float = keyframes`
  0%, 100% { transform: translateY(0); }
  50%      { transform: translateY(-4px); }
`;

const shimmer = keyframes`
  0%   { background-position: -200px 0; }
  100% { background-position: 200px 0; }
`;

const fadeInUp = keyframes`
  0%   { opacity: 0; transform: translateY(6px); }
  100% { opacity: 1; transform: translateY(0); }
`;

const PRIMARY = '#2563eb';

function ChartCard({ children, delay = 0 }: { children: React.ReactNode; delay?: number }) {
    return (
        <Box
            sx={{
                width: 96, height: 72,
                background: (t) => alpha(t.palette.background.paper, 0.82),
                border: '1px solid', borderColor: (t) => alpha(t.palette.primary.main, 0.18),
                borderRadius: 1.5,
                p: 1.25,
                boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                animation: `${fadeInUp} 0.5s ease-out both, ${float} 3.6s ease-in-out infinite`,
                animationDelay: `${delay}ms, ${delay + 200}ms`,
            }}
        >
            {children}
        </Box>
    );
}

function BarMini() {
    const heights = [18, 32, 24, 40, 28];
    return (
        <svg width="72" height="48" viewBox="0 0 72 48" aria-hidden>
            {heights.map((h, i) => (
                <rect
                    key={i}
                    x={i * 14 + 2} y={48 - h}
                    width="10" height={h}
                    rx="1.5"
                    fill={PRIMARY}
                    opacity={0.55 + (i % 3) * 0.15}
                />
            ))}
        </svg>
    );
}

function LineMini() {
    return (
        <svg width="72" height="48" viewBox="0 0 72 48" aria-hidden>
            <polyline
                points="2,38 16,26 30,30 44,14 58,20 70,8"
                fill="none" stroke={PRIMARY} strokeWidth="2"
                strokeLinecap="round" strokeLinejoin="round"
                opacity={0.78}
            />
            {[
                [2, 38], [16, 26], [30, 30], [44, 14], [58, 20], [70, 8],
            ].map(([x, y], i) => (
                <circle key={i} cx={x} cy={y} r="2" fill={PRIMARY} opacity={0.85} />
            ))}
        </svg>
    );
}

function DonutMini() {
    // 简洁环形 (stroke-dasharray)：3 段不同弧长
    const r = 18;
    const C = 2 * Math.PI * r;
    const segs = [0.45, 0.30, 0.25]; // 比例
    let offset = 0;
    return (
        <svg width="56" height="56" viewBox="0 0 56 56" aria-hidden>
            <circle cx="28" cy="28" r={r} fill="none" stroke={alpha(PRIMARY, 0.15)} strokeWidth="6" />
            {segs.map((p, i) => {
                const dash = `${C * p} ${C}`;
                const el = (
                    <circle
                        key={i}
                        cx="28" cy="28" r={r}
                        fill="none"
                        stroke={PRIMARY}
                        strokeOpacity={0.45 + i * 0.15}
                        strokeWidth="6"
                        strokeDasharray={dash}
                        strokeDashoffset={-offset}
                        transform="rotate(-90 28 28)"
                        strokeLinecap="butt"
                    />
                );
                offset += C * p;
                return el;
            })}
        </svg>
    );
}

function TableMini() {
    return (
        <svg width="72" height="48" viewBox="0 0 72 48" aria-hidden>
            {/* 表头 */}
            <rect x="2" y="4" width="68" height="10" rx="1.5" fill={alpha(PRIMARY, 0.35)} />
            {/* 数据行 */}
            {[18, 28, 38].map((y, i) => (
                <g key={i} opacity={0.65 - i * 0.08}>
                    <rect x="2" y={y} width="20" height="6" rx="1" fill={PRIMARY} />
                    <rect x="26" y={y} width="22" height="6" rx="1" fill={PRIMARY} opacity={0.65} />
                    <rect x="52" y={y} width="18" height="6" rx="1" fill={PRIMARY} opacity={0.45} />
                </g>
            ))}
        </svg>
    );
}

export function AnvilLoader() {
    return (
        <Box
            sx={{
                position: 'relative', height: '100vh', width: '100%',
                display: 'flex', flexDirection: 'column',
                alignItems: 'center', justifyContent: 'center', gap: 3,
                userSelect: 'none',
                background: (t) => `linear-gradient(180deg, ${alpha(t.palette.primary.main, 0.025)} 0%, ${alpha(t.palette.background.default, 0)} 60%)`,
            }}
        >
            <Box sx={{ display: 'flex', gap: 1.5, flexWrap: 'wrap', justifyContent: 'center' }}>
                <ChartCard delay={0}><BarMini /></ChartCard>
                <ChartCard delay={120}><LineMini /></ChartCard>
                <ChartCard delay={240}><DonutMini /></ChartCard>
                <ChartCard delay={360}><TableMini /></ChartCard>
            </Box>

            <Box sx={{ textAlign: 'center', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 0.75 }}>
                <Typography
                    sx={{
                        fontSize: 14, fontWeight: 500, letterSpacing: 1,
                        color: (t) => alpha(t.palette.text.primary, 0.78),
                        background: (t) => `linear-gradient(90deg,
                            ${alpha(t.palette.text.primary, 0.55)} 0%,
                            ${t.palette.primary.main} 50%,
                            ${alpha(t.palette.text.primary, 0.55)} 100%)`,
                        backgroundSize: '400px 100%',
                        WebkitBackgroundClip: 'text',
                        WebkitTextFillColor: 'transparent',
                        animation: `${shimmer} 2.4s linear infinite`,
                    }}
                >
                    正在加载智能分析工作台...
                </Typography>
                <Typography
                    variant="caption"
                    sx={{ color: 'text.disabled', fontSize: 12, letterSpacing: 0.5 }}
                >
                    正在准备数据与图表
                </Typography>
            </Box>
        </Box>
    );
}
