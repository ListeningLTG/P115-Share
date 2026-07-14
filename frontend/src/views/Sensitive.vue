<template>
  <div class="sensitive-container">
    <a-typography-title :level="4" style="margin-bottom: 24px">敏感词管理</a-typography-title>

    <a-tabs v-model:activeKey="activeTab">
      <!-- 选项卡 1：敏感词替换 -->
      <a-tab-pane key="replace" tab="敏感词替换">
        <div style="padding-top: 16px">
          <a-alert
            message="功能说明"
            description="此处开关仅对 TG 机器人接收并自动转存分享的链接生效。对于定时分享任务或批量转存分享任务，请在各自任务的配置中单独开启和设置敏感词替换与拼音首字母替换规则。这里的敏感词映射表是全局共享的，所有启用敏感词替换的任务都会使用本映射表。"
            type="info"
            show-icon
            style="margin-bottom: 24px"
          />

          <a-card title="替换规则配置">
            <a-form layout="vertical">
              <a-form-item label="启用敏感词替换">
                <a-switch v-model:checked="replaceEnabled" />
              </a-form-item>

              <a-form-item label="启用中文名称替换为拼音首字母">
                <template #extra>
                  <div style="font-size: 12px; color: #999; margin-top: 4px">
                    开启后，生成分享前对文件和目录的所有中文名称替换为拼音首字母。仅在启用敏感词替换时生效。
                  </div>
                </template>
                <a-switch v-model:checked="replacePinyin" :disabled="!replaceEnabled" />
              </a-form-item>

              <a-form-item label="启用 TMDB 别名替换" v-if="isTmdbConfigured">
                <template #extra>
                  <div style="font-size: 12px; color: #999; margin-top: 4px">
                    开启后，若中文名称的文件或目录具有 TMDB ID 标识，优先使用 TMDB 英文/原版别名替换其中文名称。仅在启用敏感词替换时生效。
                  </div>
                </template>
                <a-switch v-model:checked="replaceTmdb" :disabled="!replaceEnabled" />
              </a-form-item>

              <a-form-item label="敏感词映射表 (JSON 格式)">
                <template #extra>
                  <div style="font-size: 12px; color: #999; margin-top: 4px">
                    请输入一个标准的 JSON 对象格式的映射表。键为需要被替换的敏感词，值为替换后的词。
                    <br />
                    示例：<code>{"斗破苍穹": "dpcq", "凡人修仙传": "frxxz", "违规词": "xxx"}</code>
                  </div>
                </template>
                <a-textarea
                  v-model:value="replaceMappingStr"
                  placeholder='{"斗破苍穹": "dpcq", "凡人修仙传": "frxxz"}'
                  :rows="10"
                  style="font-family: monospace; font-size: 13px"
                />
              </a-form-item>

              <a-space>
                <a-button type="primary" @click="saveReplaceConfig" :loading="savingReplace">
                  保存配置
                </a-button>
                <a-button @click="validateMappingJson">
                  校验 JSON 格式
                </a-button>
              </a-space>
            </a-form>
          </a-card>
        </div>
      </a-tab-pane>
    </a-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue';
import axios from 'axios';
import { message } from 'ant-design-vue';

// Tab Key
const activeTab = ref('replace');

// Sensitive Replace State
const replaceEnabled = ref(false);
const replaceMappingStr = ref('{}');
const replacePinyin = ref(false);
const replaceTmdb = ref(false);
const savingReplace = ref(false);
const isTmdbConfigured = ref(false);

// Load sensitive replace config
const loadReplaceConfig = async () => {
  try {
    const res = await axios.get('/api/config/');
    replaceEnabled.value = res.data.sensitive_replace_enabled || false;
    replaceMappingStr.value = res.data.sensitive_replace_mapping || '{}';
    replacePinyin.value = res.data.sensitive_replace_pinyin || false;
    replaceTmdb.value = res.data.sensitive_replace_tmdb || false;
    isTmdbConfigured.value = !!res.data.tmdb_api_key;
  } catch (e) {
    console.error('加载敏感词替换配置失败:', e);
    message.error('加载替换配置失败');
  }
};

// Validate JSON syntax and structure
const validateMappingJson = () => {
  try {
    const parsed = JSON.parse(replaceMappingStr.value);
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      message.error('映射表必须是一个 JSON 对象 (例如: {"敏感词": "替换词"})');
      return false;
    }
    for (const [k, v] of Object.entries(parsed)) {
      if (typeof k !== 'string' || typeof v !== 'string') {
        message.warning('映射表的键和值都应为字符串格式');
        return true;
      }
    }
    message.success('JSON 校验通过！格式正确。');
    return true;
  } catch (err: any) {
    message.error(`JSON 语法解析错误: ${err.message}`);
    return false;
  }
};

// Save sensitive replace config
const saveReplaceConfig = async () => {
  if (!validateMappingJson()) {
    return;
  }
  try {
    savingReplace.value = true;
    const res = await axios.post('/api/config/update', {
      sensitive_replace_enabled: replaceEnabled.value,
      sensitive_replace_mapping: replaceMappingStr.value,
      sensitive_replace_pinyin: replacePinyin.value,
      sensitive_replace_tmdb: replaceTmdb.value
    });
    if (res.data.status === 'success') {
      message.success('替换配置已保存');
    } else {
      message.error(res.data.message || '保存失败');
    }
  } catch (e: any) {
    message.error(e.response?.data?.detail || '保存失败');
  } finally {
    savingReplace.value = false;
  }
};

onMounted(async () => {
  await loadReplaceConfig();
});
</script>

<style scoped>
.sensitive-container {
  height: 100%;
  overflow-y: auto;
  padding-right: 8px;
}
</style>
