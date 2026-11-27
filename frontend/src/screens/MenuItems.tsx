import { useState } from "react"
import { CookingPot } from "lucide-react"
import { ResourceScreen } from "../components/ResourceScreen"
import { Button } from "../components/ui/button"
import { useAuth } from "../lib/auth"
import { menuItemsConfig } from "./configs"
import RecipeManager from "./RecipeManager"
import { t as translate, useUILanguage } from "../lib/i18n"

const MENU_WRITE_ROLES = [
  "Finance",
  "F&B Manager",
  "Hotel Admin",
  "System Manager",
  "Administrator",
]

export default function MenuItems() {
  useUILanguage()
  const { roles } = useAuth()
  const canEditRecipes = roles.some((r) => MENU_WRITE_ROLES.includes(r))
  const [recipes, setRecipes] = useState(false)

  return (
    <>
      <ResourceScreen
        config={menuItemsConfig}
        headerAction={
          canEditRecipes ? (
            <Button variant="outline" onClick={() => setRecipes(true)}>
              <CookingPot className="mr-1 size-4" aria-hidden />{translate("Recipes")}
            </Button>
          ) : undefined
        }
      />
      {recipes && <RecipeManager onClose={() => setRecipes(false)} />}
    </>
  )
}
