'use client';

import React from 'react';
import dynamic from 'next/dynamic';
import { SERIES_COLORS } from '@/lib/charts';

// Dynamically import ApexCharts to avoid SSR issues
const Chart = dynamic(() => import('react-apexcharts'), { ssr: false });

export interface PieChartData {
  name: string;
  value: number;
  /** The entity's colour; defaults to the categorical slot for its position. */
  color?: string;
}

export interface PieChartProps {
  data: PieChartData[];
  size?: number | string;
  showLabels?: boolean;
  showLegend?: boolean;
  className?: string;
  title?: string;
  /**
   * Formats a value with its unit (e.g. "3 requests", "$1,234.00"). When set,
   * the legend lists each value and the tooltip adds the share. Omit for
   * placeholder charts, whose numbers are not real.
   */
  formatValue?: (value: number) => string;
}

export function PieChart({
  data,
  size = 300,
  showLabels = true,
  showLegend = true,
  className = '',
  title = '',
  formatValue,
}: PieChartProps) {
  // Safety check for data
  if (!data || data.length === 0) {
    return (
      <div className={`flex w-full items-center justify-center ${className}`}>
        <p className="text-gray-400">No data available</p>
      </div>
    );
  }

  const total = data.reduce((sum, d) => sum + d.value, 0);
  const share = (v: number) => (total > 0 ? `${Math.round((v / total) * 100)}%` : '');

  const chartOptions: ApexCharts.ApexOptions = {
    chart: {
      type: 'pie',
      background: 'transparent',
      animations: {
        enabled: true,
      },
    },
    labels: data.map((item) => item.name),
    colors: data.map((item, i) => item.color ?? SERIES_COLORS[i % SERIES_COLORS.length]),
    // A 2px surface gap between slices.
    stroke: { width: 2, colors: ['#ffffff'] },
    legend: {
      show: showLegend,
      position: 'bottom',
      labels: {
        colors: '#4b5563',
      },
      // Values stay readable without hovering or telling colours apart.
      formatter: formatValue
        ? (name, opts) => `${name} · ${formatValue(data[opts.seriesIndex]?.value ?? 0)}`
        : undefined,
    },
    dataLabels: {
      enabled: showLabels,
      style: {
        colors: ['#ffffff'],
      },
    },
    plotOptions: {
      pie: {
        expandOnClick: false,
        donut: {
          labels: {
            show: false,
          },
        },
      },
    },
    theme: {
      mode: 'light',
    },
    noData: {
      text: 'No data available',
      style: {
        color: '#9ca3af',
      },
    },
    tooltip: {
      enabled: !!formatValue,
      y: {
        formatter: (val) => (formatValue ? `${formatValue(val)} (${share(val)})` : String(val)),
      },
    },
  };

  const series = data.map((item) => item.value);

  return (
    <div className={`w-full ${className}`}>
      {title && <h3 className="text-lg font-semibold text-gray-900 mb-4 text-center">{title}</h3>}
      <Chart options={chartOptions} series={series} type="pie" width={size} height={size} />
    </div>
  );
}
