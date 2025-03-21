from flask import (
    Blueprint,
    render_template,
    request,
    flash,
    jsonify,
    redirect,
    url_for,
    send_file,
    session,
)
from flask_login import login_required, current_user
from .models import Recipe, Ingredient, Favorite, RecipeIngredient
from . import db
from .utils.db_utils import (
    get_recipe_by_id,
    get_recipes_by_ingredient,
    get_recipe_with_details,
    add_favorite,
    remove_favorite,
    get_user_favorites,
)
import json
import os
from werkzeug.utils import secure_filename
from PIL import Image
import io
from .utils.object_detection import ObjectDetector

views = Blueprint("views", __name__)
detector = ObjectDetector()

# Map YOLO class IDs to ingredient names
INGREDIENT_MAP = {
    0: "aubergine",
    1: "cabbage",
    2: "carrot",
    3: "cauliflower",
    4: "garlic",
    5: "green-pepper",
    6: "onion",
    7: "patato",
    8: "spinach",
    9: "tomato",
}


@views.route("/", methods=["GET"])
def home():
    return render_template("home.html", user=current_user)


@views.route("/upload-image", methods=["POST"])
def upload_image():
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400

    file = request.files["image"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    if file:
        try:
            image_bytes = file.read()

            try:
                detections = detector.detect_ingredients(image_bytes)
                print(f"Detections: {detections}")
                class_ids_list = [int(d["class"]) for d in detections]

            except Exception as e:
                print(f"Error in detect_ingredients: {str(e)}")
                import traceback

                print(f"Traceback: {traceback.format_exc()}")
                return jsonify({"error": f"Error detecting ingredients: {str(e)}"}), 500

            try:
                annotated_image = detector.detect_and_draw(image_bytes)
                print("Annotated image received")
            except Exception as e:
                print(f"Error in detect_and_draw: {str(e)}")
                import traceback

                print(f"Traceback: {traceback.format_exc()}")
                return (
                    jsonify({"error": f"Error creating annotated image: {str(e)}"}),
                    500,
                )

            detected_ingredients = []
            for class_id in class_ids_list:
                if class_id in INGREDIENT_MAP:
                    detected_ingredients.append(
                        {
                            "name": INGREDIENT_MAP[class_id],
                            "confidence": next(
                                (
                                    d["confidence"]
                                    for d in detections
                                    if d["class"] == class_id
                                ),
                                0.0,
                            ),
                            "bbox": next(
                                (
                                    d["bbox"]
                                    for d in detections
                                    if d["class"] == class_id
                                ),
                                [],
                            ),
                        }
                    )

            if not detected_ingredients:
                return jsonify({"error": "No ingredients detected"}), 400

            session["detected_ingredients"] = [
                ing["name"] for ing in detected_ingredients
            ]

            return jsonify(
                {
                    "success": True,
                    "ingredients": detected_ingredients,
                    "annotated_image": annotated_image.decode("latin1"),
                }
            )

        except Exception as e:
            print(f"Error processing image: {str(e)}")
            import traceback

            print(f"Traceback: {traceback.format_exc()}")
            return jsonify({"error": str(e)}), 500

    return jsonify({"error": "Invalid image file"}), 400


from sqlalchemy import text


def find_recipes_by_ingredients(ingredients):
    """Find recipes based on detected ingredients."""
    try:
        print(f"Searching for recipes with ingredients: {ingredients}")

        ingredient_names = ", ".join(f"'{ingredient}'" for ingredient in ingredients)
        query = f"""
        WITH UserIngredients AS (
            SELECT ingr_id 
            FROM dbo.Ingredients 
            WHERE ingr_name IN ({ingredient_names})
        )
        SELECT r.recipe_id, r.name, COUNT(ri.ingr_id) AS match_count, r.time, r.calories
        FROM dbo.Recipes r
        JOIN dbo.Recipe_Ingredient ri ON r.recipe_id = ri.recipe_id
        JOIN UserIngredients ui ON ri.ingr_id = ui.ingr_id
        GROUP BY r.recipe_id, r.name, r.time, r.calories
        ORDER BY match_count DESC, r.time ASC, r.calories ASC;
        """

        result = db.session.execute(text(query)).fetchall()
        recipe_list = []

        for row in result:
            recipe_list.append(
                {
                    "recipe_id": row.recipe_id,
                    "name": row.name,
                    "match_count": row.match_count,
                    "time": row.time,
                    "calories": row.calories,
                }
            )

        print(f"Found {len(recipe_list)} unique recipes")
        return recipe_list

    except Exception as e:
        print(f"Error fetching recipes: {e}")
        import traceback

        print(f"Traceback: {traceback.format_exc()}")
        return []


@views.route("/favorites", methods=["GET"])
@login_required
def favorites():
    favorite_recipes = get_user_favorites(current_user.user_id)
    recipes_with_details = [
        get_recipe_with_details(recipe.recipe_id) for recipe in favorite_recipes
    ]
    return render_template(
        "favorites.html", user=current_user, recipes=recipes_with_details
    )


@views.route("/add-favorite/<int:recipe_id>", methods=["POST"])
@login_required
def add_favorite_route(recipe_id):
    recipe = get_recipe_by_id(recipe_id)

    if not recipe:
        flash("Recipe not found.", category="error")
        return redirect(url_for("views.home"))

    add_favorite(current_user.user_id, recipe_id)
    flash("Recipe added to favorites!", category="success")
    return redirect(url_for("views.favorites"))


@views.route("/remove-favorite/<int:recipe_id>", methods=["POST"])
@login_required
def remove_favorite_route(recipe_id):
    if remove_favorite(current_user.user_id, recipe_id):
        flash("Recipe removed from favorites.", category="success")
    else:
        flash("Recipe not in favorites.", category="error")

    return redirect(url_for("views.favorites"))


@views.route("/recipes")
def recipes():
    detected_ingredients = session.get("detected_ingredients", [])
    if not detected_ingredients:
        flash("No ingredients detected. Please upload an image first.", "error")
        return redirect(url_for("views.home"))

    recipes = find_recipes_by_ingredients(detected_ingredients)
    return render_template(
        "recipe_results.html",
        user=current_user,
        ingredients=detected_ingredients,
        recipes=recipes,
    )


@views.route("/all-ingredients")
def all_ingredients():
    ingredients = session.get("detected_ingredients", [])
    return render_template("all_ingredients.html", ingredients=ingredients)


@views.route("/add-ingredient", methods=["POST"])
def add_ingredient():
    try:
        data = request.get_json()
        ingredient = data.get("ingredient")

        if not ingredient:
            return jsonify({"success": False, "error": "No ingredient specified"}), 400

        current_ingredients = session.get("detected_ingredients", [])

        if ingredient not in current_ingredients:
            current_ingredients.append(ingredient)
            session["detected_ingredients"] = current_ingredients

        return jsonify({"success": True})

    except Exception as e:
        print(f"Error adding ingredient: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500
