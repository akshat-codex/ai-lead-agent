import { redirect } from "next/navigation";

export default function LeadsIndexPage() {
  redirect("/leads/new");
}
