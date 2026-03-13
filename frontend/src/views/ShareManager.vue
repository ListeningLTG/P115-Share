<template>
  <div class="share-manager">
    <div class="header-actions">
      <div class="header-left">
        <!-- 账号选择器 -->
        <a-select
          v-model:value="selectedAccountId"
          style="width: 180px"
          placeholder="选择账号"
          @change="onAccountChange"
          :loading="accountsLoading"
        >
          <a-select-option v-for="acc in accounts" :key="acc.id" :value="acc.id">
            P{{ acc.priority }} {{ acc.name }}
          </a-select-option>
        </a-select>

        <!-- 操作按钮区（始终显示，最左侧） -->
        <a-button type="primary" :loading="isAnalyzing || loading" @click="startFullAnalysis">
          <template #icon><ReloadOutlined /></template>
          {{ isAnalyzing ? '正在扫描...' : '一键分析' }}
        </a-button>
        <a-button :disabled="isAnalyzing || statsTotal === 0" @click="resetAnalysis">
          <template #icon><DeleteOutlined /></template>
          重置
        </a-button>
        <a-dropdown :disabled="isAnalyzing || statsTotal === 0">
          <a-button>
            <template #icon><DownloadOutlined /></template>
            导出
          </a-button>
          <template #overlay>
            <a-menu @click="handleExport">
              <a-menu-item key="json">导出为 JSON</a-menu-item>
              <a-menu-item key="excel">导出为 Excel</a-menu-item>
            </a-menu>
          </template>
        </a-dropdown>
        <div v-if="isAnalyzing" style="font-size: 12px; color: #999">
          扫描中: {{ scannedCount }} / {{ statsTotal }} ({{ statsTotal > 0 ? Math.round(scannedCount / statsTotal * 100) : 0 }}%)
        </div>

        <!-- 搜索和筛选：仅在有分析结果时显示 -->
        <template v-if="statsTotal > 0">
          <a-input-search
            v-model:value="searchText"
            placeholder="搜索分享名称..."
            style="width: 260px"
            :disabled="isAnalyzing"
            @search="onSearch"
          />
          <div class="filter-box">
            <span style="margin-right: 8px">状态筛选:</span>
            <a-select
              v-model:value="statusFilter"
              style="width: 130px"
              :disabled="isAnalyzing"
              @change="onFilterChange"
            >
              <a-select-option value="all">全部</a-select-option>
              <a-select-option value="normal">正常</a-select-option>
              <a-select-option value="violated">违规</a-select-option>
              <a-select-option value="expired">已过期</a-select-option>
              <a-select-option value="reviewing">审核中</a-select-option>
            </a-select>
          </div>
        </template>
      </div>

      <!-- 汇总统计：仅在有分析结果时显示 -->
      <div class="summary-stats" v-if="statsTotal > 0">
        <a-statistic title="总分享数" :value="statsTotal" style="margin-right: 20px" />
        <a-statistic title="正常" :value="statsNormal" :value-style="{ color: '#52c41a' }" style="margin-right: 20px" />
        <a-statistic title="违规" :value="statsViolated" :value-style="{ color: '#ff4d4f' }" style="margin-right: 20px" />
        <a-statistic title="已过期" :value="statsExpired" :value-style="{ color: '#fa8c16' }" style="margin-right: 20px" />
        <a-statistic title="审核中" :value="statsReviewing" :value-style="{ color: '#1890ff' }" />
      </div>
    </div>

    <a-table
      :columns="columns"
      :data-source="shareData"
      :loading="loading"
      :pagination="pagination"
      @change="handleTableChange"
      row-key="id"
      size="middle"
    >
      <template #title v-if="lastUpdated">
        <div style="font-size: 12px; color: #999">
          上次分析时间: {{ lastUpdated }}
        </div>
      </template>
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'share_title'">
          <a :href="record.share_url" target="_blank">{{ record.share_title }}</a>
        </template>
        <template v-if="column.key === 'status_text'">
          <a-tag :color="getStatusColor(record)">
            {{ record.status_text }}
          </a-tag>
        </template>
        <template v-if="column.key === 'actions'">
          <a-button type="link" size="small" @click="openLink(record)">打开链接</a-button>
        </template>
      </template>
    </a-table>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { ReloadOutlined, DeleteOutlined, DownloadOutlined } from '@ant-design/icons-vue';
import { message, Modal } from 'ant-design-vue';
import axios from 'axios';

interface Account {
  id: number;
  name: string;
  priority: number;
}

const loading = ref(false);
const isAnalyzing = ref(false);
const shareData = ref([]);
const searchText = ref('');
const statusFilter = ref('all');

const statsTotal = ref(0);
const statsNormal = ref(0);
const statsViolated = ref(0);
const statsExpired = ref(0);
const statsReviewing = ref(0);
const scannedCount = ref(0);
const lastUpdated = ref('');
const sortField = ref('create_time');
const sortOrder = ref('descend');
let pollTimer: any = null;

// 账号选择
const accounts = ref<Account[]>([]);
const accountsLoading = ref(false);
const selectedAccountId = ref<number | undefined>(undefined);

const loadAccounts = async () => {
  accountsLoading.value = true;
  try {
    const res = await axios.get('/api/accounts/');
    accounts.value = res.data.accounts || [];
    if (accounts.value.length > 0 && !selectedAccountId.value) {
      selectedAccountId.value = accounts.value[0].id;
    }
  } catch (e) {
    // ignore
  } finally {
    accountsLoading.value = false;
  }
};

const onAccountChange = async () => {
  // 重置统计
  statsTotal.value = 0; statsNormal.value = 0; statsViolated.value = 0;
  statsExpired.value = 0; statsReviewing.value = 0; scannedCount.value = 0;
  lastUpdated.value = ''; shareData.value = [];
  pagination.value.total = 0;
  // 加载新账号的状态
  await pollAnalysisStatus();
  if (!isAnalyzing.value && statsTotal.value > 0) {
    fetchShares(1, pagination.value.pageSize);
  }
};

const columns = [
  { title: '分享名称', dataIndex: 'share_title', key: 'share_title', sorter: true },
  { title: '状态', dataIndex: 'status_text', key: 'status_text', width: 110, sorter: true },
  { title: '大小', dataIndex: 'size_text', key: 'size_text', width: 120 },
  { title: '分享时间', dataIndex: 'create_time', key: 'create_time', width: 180, sorter: true, defaultSortOrder: 'descend' },
  { title: '接收次数', dataIndex: 'receive_count', key: 'receive_count', width: 100, sorter: true },
  { title: '操作', key: 'actions', width: 100 }
];

const pagination = ref({
  current: 1,
  pageSize: 10,
  total: 0,
  showSizeChanger: true,
  showTotal: (total: number) => `共 ${total} 条项目`,
});

const getStatusColor = (record: any) => {
  if (record.is_violated) return 'error';
  if (record.is_expired) return 'warning';
  if (record.is_reviewing) return 'processing';
  return 'success';
};

const fetchShares = async (page = 1, pageSize = 10) => {
  loading.value = true;
  try {
    let orderField = sortField.value;
    if (sortField.value === 'status_text') orderField = 'share_state';

    const response = await axios.get('/api/share/list', {
      params: {
        limit: pageSize,
        offset: (page - 1) * pageSize,
        order: orderField,
        asc: sortOrder.value === 'ascend' ? 1 : 0,
        search_value: searchText.value || undefined,
        status_filter: statusFilter.value,
        account_id: selectedAccountId.value,
      }
    });

    if (response.data.state) {
      shareData.value = response.data.list;
      pagination.value.total = response.data.count;
      pagination.value.current = page;
      pagination.value.pageSize = pageSize;
    } else {
      message.error(response.data.error || '获取数据失败');
    }
  } catch (error) {
    message.error('加载本地分析结果失败');
  } finally {
    loading.value = false;
  }
};

const pollAnalysisStatus = async () => {
  try {
    const response = await axios.get('/api/share/analysis-status', {
      params: { account_id: selectedAccountId.value }
    });
    const data = response.data;

    isAnalyzing.value = data.is_analyzing;
    statsTotal.value = data.total;
    statsNormal.value = data.normal;
    statsViolated.value = data.violated;
    statsExpired.value = data.expired ?? 0;
    statsReviewing.value = data.reviewing ?? 0;
    scannedCount.value = data.scanned;
    lastUpdated.value = data.last_updated;

    if (!data.is_analyzing) {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
        message.success('全量分析完成');
      }
      fetchShares(pagination.value.current, pagination.value.pageSize);
    }
  } catch (error) {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    isAnalyzing.value = false;
  }
};

const startFullAnalysis = async () => {
  if (isAnalyzing.value) return;
  try {
    const response = await axios.post('/api/share/analyze', null, {
      params: { account_id: selectedAccountId.value }
    });
    if (response.data.state) {
      isAnalyzing.value = true;
      scannedCount.value = 0;
      statsNormal.value = 0; statsViolated.value = 0;
      statsExpired.value = 0; statsReviewing.value = 0;
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(pollAnalysisStatus, 2000);
      message.info('分析任务已在后台启动');
    } else {
      message.error(response.data.error || '启动分析失败');
    }
  } catch (error) {
    message.error('请求失败');
  }
};

const resetAnalysis = () => {
  Modal.confirm({
    title: '确认重置',
    content: '将清除当前账号的所有分析结果，是否继续？',
    okText: '确认重置',
    okType: 'danger',
    cancelText: '取消',
    onOk: async () => {
      try {
        const response = await axios.post('/api/share/reset', null, {
          params: { account_id: selectedAccountId.value }
        });
        if (response.data.state) {
          statsTotal.value = 0; statsNormal.value = 0; statsViolated.value = 0;
          statsExpired.value = 0; statsReviewing.value = 0;
          scannedCount.value = 0; lastUpdated.value = '';
          shareData.value = []; pagination.value.total = 0;
          message.success('分析结果已重置');
        }
      } catch (error) {
        message.error('重置失败');
      }
    }
  });
};

const onSearch = () => { fetchShares(1, pagination.value.pageSize); };
const onFilterChange = () => { fetchShares(1, pagination.value.pageSize); };

const handleTableChange = (pag: any, _filters: any, sorter: any) => {
  if (sorter.field) { sortField.value = sorter.field; sortOrder.value = sorter.order || 'descend'; }
  fetchShares(pag.current, pag.pageSize);
};

const openLink = (record: any) => {
  const url = record.receive_code ? `${record.share_url}?password=${record.receive_code}` : record.share_url;
  window.open(url, '_blank');
};

const handleExport = async ({ key }: { key: string }) => {
  try {
    const params = new URLSearchParams({ status_filter: statusFilter.value });
    if (searchText.value) params.append('search_value', searchText.value);
    if (selectedAccountId.value) params.append('account_id', String(selectedAccountId.value));
    const url = `/api/share/export/${key}?${params.toString()}`;
    const link = document.createElement('a');
    link.href = url; link.download = '';
    document.body.appendChild(link); link.click(); document.body.removeChild(link);
    message.success(`正在导出为 ${key.toUpperCase()} 文件...`);
  } catch (error) {
    message.error('导出失败');
  }
};

onMounted(async () => {
  await loadAccounts();
  await pollAnalysisStatus();
  if (isAnalyzing.value) {
    pollTimer = setInterval(pollAnalysisStatus, 2000);
  }
});
</script>

<style scoped>
.share-manager {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.header-actions {
  margin-bottom: 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.filter-box {
  display: flex;
  align-items: center;
}

.summary-stats {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
}
</style>
