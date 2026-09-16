// 模板示例页:新宿主接入真实列表后，删掉 app/[locale]/app/examples 整个目录。
import { TableExample } from "../../../../../components/examples/table-example";
export default async function ExampleTablePage({ params }: { params: Promise<{ locale: string }> }) { const { locale } = await params; return <TableExample locale={locale}/>; }
